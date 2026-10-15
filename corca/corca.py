"""CoRCA: iterative explain-away around any single-root-cause RCA method.

    remaining = all metrics
    repeat (at most max_rounds):
        r = top service of base_method(remaining)
        record r as a root cause
        drop the metrics r explains (r + its transitive callers)
        stop if nothing in `remaining` is anomalous above tau   (never before round 1)

Design choices you should be able to defend in the report:
  * Explain-away uses the *service dependency graph*, not a learned causal graph.
    It is cheap and auditable; the cost is that a second root cause sitting on a
    caller of the first can be hidden (see keep_resource).
  * keep_resource=True: for explained callers we only drop symptom metrics
    (latency, errors, workload, socket) and keep their own cpu/mem/disk, because a
    downstream fault rarely raises a caller's resource usage. That lets a CPU hog
    on e.g. checkoutservice survive after currencyservice is explained.
    Ablate with keep_resource=False.
  * The stop threshold tau is on a method-independent anomaly score
    (max robust z, as in BARO) so the same stopping rule works for BARO and CIRCA.
    Calibrate it on single-fault cases with calibrate_tau() - don't hand-tune.
"""
from dataclasses import dataclass, field
from functools import partial

import pandas as pd

from .base import anomaly_scores, rank_metrics, service_scores, services_from_metrics
from .execution import call_with_timeout
from .topology import RESOURCE_SUFFIXES, affected_by, call_graph, metric_kind, service_of


STOPPING_POLICY = "residual-strictly-above-tau-v1"


@dataclass
class Round:
    service: str          # root cause found this round
    score: float          # its max anomaly score (for ranking / confidence)
    confidence: float     # score share vs next strongest service, 0..1
    explained: list       # services whose symptom metrics were removed
    residual: float       # top anomaly score left after removal (inf = not computed)


@dataclass
class CoRCAResult:
    root_causes: list = field(default_factory=list)
    rounds: list = field(default_factory=list)
    timed_out: bool = False       # a round exceeded round_timeout; later rounds skipped
    remaining_anomaly_score: float = 0.0
    remaining_service_scores: dict = field(default_factory=dict)


def _explained_columns(cols, root, graph, keep_resource):
    explained = affected_by(root, graph)
    drop = []
    for c in cols:
        s = service_of(c)
        if s == root:
            drop.append(c)                        # all metrics of the root itself
        elif s in explained:
            if keep_resource and metric_kind(c).startswith(RESOURCE_SUFFIXES):
                continue                          # caller's own resources stay candidates
            drop.append(c)
    return drop, sorted(explained - {root})


def corca(df: pd.DataFrame, inject_time: int, method="baro", graph=None,
          max_rounds=3, tau=None, keep_resource=True, dataset="re2-ob",
          rank_fn=None, round_timeout=None, initial_rank=None) -> CoRCAResult:
    """Find up to `max_rounds` root-cause services.

    tau=None   -> always run max_rounds (useful for computing ranked lists / calibration)
    round_timeout -> seconds allowed per round; on timeout CoRCA stops and keeps
                  what it has found (res.timed_out=True). Report how often this happens.
    rank_fn    -> optional custom ranker (df, inject_time) -> ranked metric names;
                  overrides `method`, used by the tests to avoid needing RCAEval.
                  Must be picklable when round_timeout is enabled.
    initial_rank -> optional metric ranking already computed for the full df;
                  reuses the baseline as round 1 without running it twice.
    """
    graph = graph if graph is not None else call_graph()
    if max_rounds < 1:
        raise ValueError("max_rounds must be positive")
    if round_timeout is not None and round_timeout <= 0:
        raise ValueError("round_timeout must be positive or None")
    rank = rank_fn or partial(rank_metrics, method, dataset=dataset)

    work = df.copy()
    res = CoRCAResult()
    scores = anomaly_scores(work, inject_time)

    for index in range(max_rounds):
        candidates = [c for c in work.columns if c != "time"]
        if not candidates:
            break
        # Round 1 always runs: we are only called because an alert fired, so there is
        # at least one root cause. tau only decides whether to look for *more*.
        if tau is not None and res.root_causes and (scores.empty or scores.iloc[0] <= tau):
            break                                 # nothing significant left to explain

        try:
            metrics = initial_rank if index == 0 and initial_rank is not None else call_with_timeout(
                rank, round_timeout, (work, inject_time))
            ranked = services_from_metrics([m for m in metrics if m in candidates])
        except TimeoutError:
            res.timed_out = True
            break
        ranked = [s for s in ranked if s not in res.root_causes]
        if not ranked:
            break
        root = ranked[0]

        svc_scores = service_scores(scores)
        root_score = float(svc_scores.get(root, 0.0))
        next_score = float(svc_scores.drop(index=root, errors="ignore").iloc[0]) if len(svc_scores) > 1 else 0.0
        confidence = root_score / (root_score + max(next_score, 0.0)) if root_score > 0 else 0.0
        drop, explained = _explained_columns(candidates, root, graph, keep_resource)
        work = work.drop(columns=drop)
        scores = scores.drop(index=[c for c in drop if c in scores.index])

        residual = float(scores.iloc[0]) if not scores.empty else 0.0
        res.root_causes.append(root)
        res.rounds.append(Round(root, root_score, confidence, explained, residual))
    res.remaining_anomaly_score = float(scores.iloc[0]) if not scores.empty else 0.0
    res.remaining_service_scores = {s: float(v) for s, v in service_scores(scores).items()}
    return res


def calibrate_tau(single_fault_results: list, quantile=0.9) -> float:
    """Pick tau from *single-fault* runs made with tau=None.

    After the true (single) root cause is explained, whatever anomaly is left is
    noise/propagation we failed to explain. Setting tau at the `quantile` of those
    round-1 residuals means CoRCA wrongly reports a 2nd root cause on roughly
    (1 - quantile) of single-fault cases - a false-alarm budget you choose explicitly.
    Only use results where round 1 was correct, otherwise the residual contains the real fault.
    """
    residuals = [r.rounds[0].residual for r in single_fault_results if r.rounds]
    if not residuals:
        raise ValueError("no rounds to calibrate on")
    return float(pd.Series(residuals).quantile(quantile))
