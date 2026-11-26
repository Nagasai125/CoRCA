"""Thin adapters around RCAEval's base RCA methods.

Every base method takes (df, inject_time) and returns a ranked list of *metric*
columns. CoRCA works at service level, so we collapse that into a ranked list
of services (first occurrence wins), the same way RCAEval scores 'service' accuracy.
"""
import random

import numpy as np
import pandas as pd

from .topology import metric_kind, service_of
from .circa import rank_circa


def _rcaeval(name):
    # imported lazily so the rest of the package (and the tests) work without RCAEval
    import RCAEval.e2e as e2e
    return getattr(e2e, name)


def rank_metrics(method: str, df: pd.DataFrame, inject_time: int, dataset: str = "re2-ob", seed=0) -> list:
    """Run an RCAEval method ('baro', 'circa', ...) and return ranked metric names."""
    if "time" not in df.columns:
        raise ValueError("df must contain a 'time' column")
    random.seed(seed)
    np.random.seed(seed)
    if method == "circa":
        return rank_circa(df, inject_time, dataset)
    out = _rcaeval(method)(df.copy(), inject_time, dataset=dataset, dk_select_useful=False)
    # RCAEval's error decorator returns adj=[] and unranked columns on failure.
    if "adj" in out and len(out["adj"]) == 0:
        raise RuntimeError(f"{method} returned RCAEval's fallback ranking; method failed")
    return [m for m in out["ranks"] if m != "time"]


def services_from_metrics(ranked_metrics: list) -> list:
    """['cartservice_cpu', 'frontend_latency-90', 'cartservice_mem'] -> ['cartservice', 'frontend']"""
    seen, out = set(), []
    for m in ranked_metrics:
        s = service_of(m)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def service_scores(scores: pd.Series) -> pd.Series:
    """Collapse metric anomaly scores to one max score per service."""
    out = {}
    for metric, score in scores.items():
        svc = service_of(metric)
        out[svc] = max(out.get(svc, float("-inf")), float(score))
    return pd.Series(out, dtype=float).sort_values(ascending=False)


def anomaly_scores(df: pd.DataFrame, inject_time: int, q: float = 0.9) -> pd.Series:
    """Per-metric, unit-free anomaly score used by CoRCA's stopping rule.

    z = (x - median_normal) / sigma, with
        sigma = max(1.4826*MAD, std, 1% of |median|) over the normal window.
    The floor matters on real data: RE1-OB memory is in bytes and often flat
    before the fault, so a plain robust z (BARO's scaler, IQR=0) turns a 1 MB
    wobble into a score of ~10^6 and no threshold makes sense.
    Score = q-quantile of z after injection (a *sustained* shift; a single spike
    does not count), so q=0.9 means 'at least 10% of the window is this high'.
    """
    if "time" not in df.columns:
        raise ValueError("df must contain a 'time' column")
    normal = df[df["time"] < inject_time].drop(columns="time")
    anomal = df[df["time"] >= inject_time].drop(columns="time")
    if normal.empty or anomal.empty:
        raise ValueError("df must contain samples before and after inject_time")
    scores = {}
    for c in normal.columns:
        a, b = normal[c].to_numpy(float), anomal[c].to_numpy(float)
        a, b = a[np.isfinite(a)], b[np.isfinite(b)]
        if not len(a) or not len(b):
            continue
        if np.std(a) == 0 and np.std(b) == 0 and a[0] == b[0]:
            continue                                   # constant metric: no information
        med = np.nanmedian(a)
        # ponytail: fixed noise floors for fractions/counts; estimate sampling
        # uncertainty when request/event exposure counts are available.
        kind = metric_kind(c)
        floor = {"error": 0.01, "trace-error": 0.01, "log-error": 1.0, "workload": 1.0}.get(kind, 1e-9)
        sigma = max(1.4826 * np.nanmedian(np.abs(a - med)), np.nanstd(a), 0.01 * abs(med), floor)
        scores[c] = float(np.nanquantile((b - med) / sigma, q))
    return pd.Series(scores, dtype=float).sort_values(ascending=False)
