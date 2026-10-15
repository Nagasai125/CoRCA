"""PC/RHT adapter using observed windows, rather than RCAEval's padded window."""
from types import SimpleNamespace

import numpy as np


FEATURE_POLICY = "pc-rht-observed-no-workload-v1"


class InsufficientEvidence(ValueError):
    """The observed telemetry cannot support the requested diagnosis."""


def rank_circa(df, inject_time, dataset, *, scorer=None):
    from RCAEval.classes.graph import MemoryGraph, Node
    from RCAEval.graph_construction.pc import pc_default
    from RCAEval.graph_heads.rht import RHTScorer
    from RCAEval.io.time_series import preprocess

    timestamps = df["time"].to_numpy(float)
    if (not np.isfinite(inject_time) or not np.isfinite(timestamps).all()
            or np.any(np.diff(timestamps) <= 0)):
        raise ValueError("circa timestamps must be finite and strictly increasing")
    # Shared request counts can be deterministic across services. Exclude the
    # entire metric family, never individual streams selected using fault truth.
    graph_input = df.drop(columns=[c for c in df if c.endswith("_workload")])
    if len(graph_input.columns) <= 1:
        raise InsufficientEvidence("circa has no graph metrics after excluding workload counts")
    data = preprocess(graph_input.copy(), dataset=dataset, dk_select_useful=False)
    data = data.drop(columns="time", errors="ignore")
    names = data.columns.tolist()
    normal = int((df["time"] < inject_time).sum())
    anomalous = len(df) - normal
    if not names or min(normal, anomalous) < 10:
        raise InsufficientEvidence("circa requires varying metrics and at least 10 observed samples in each window")
    # Conservative gate: even a regression on every other metric must be estimable.
    if normal <= len(names) + 2:
        raise InsufficientEvidence(
            f"circa has {normal} normal samples for {len(names)} varying metrics; "
            f"collect more than {len(names) + 2} normal samples (longer windows), not interpolated copies")
    values = data.to_numpy(float)
    if not np.isfinite(values).all():
        raise InsufficientEvidence("circa requires finite metric values after missing-data preparation")
    if len(names) > 1:
        correlation = np.corrcoef(values.T)
        if np.linalg.matrix_rank(correlation) < len(names):
            pairs = [(names[i], names[j]) for i in range(len(names)) for j in range(i + 1, len(names))
                     if np.isclose(abs(correlation[i, j]), 1, rtol=0, atol=1e-12)]
            raise InsufficientEvidence(
                f"circa correlation matrix is singular; deterministic metric dependencies must be resolved "
                f"in the telemetry schema before Fisher-Z graph learning; correlated pairs: {pairs[:5]}")
    try:
        adjacency = pc_default(data) if len(names) > 1 else np.zeros((1, 1), dtype=int)
    except ValueError as exc:
        if "singular" in str(exc).lower():
            raise InsufficientEvidence(f"circa Fisher-Z graph learning: {exc}") from exc
        raise
    nodes = [Node(*name.split("_", 1)) for name in names]
    graph = MemoryGraph.from_adj(adjacency, nodes)
    series = {node: values[:, index] for index, node in enumerate(nodes)}
    # RHTScorer needs window lengths and load_data. Supply only actual observations:
    # no 1s resampling, no extrapolation to inject_time+300, and keep isolated nodes.
    window = SimpleNamespace(train_window=normal, test_window=anomalous,
                             load_data=lambda graph, current: series)
    scorer = RHTScorer() if scorer is None else scorer
    scores = scorer.score(graph=graph, data=window, current=float(df["time"].iloc[-1]))
    ranked = sorted(scores, key=lambda node: (-scores[node].score, node.entity, node.metric))
    if any(not np.isfinite(score.score) for score in scores.values()):
        raise InsufficientEvidence("circa produced non-finite regression scores")
    return [f"{node.entity}_{node.metric}" for node in ranked]
