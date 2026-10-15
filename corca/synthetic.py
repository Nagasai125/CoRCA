"""Synthetic Online Boutique telemetry with 1..N concurrent faults.

Purpose: a fast sanity check for the CoRCA loop and the evaluation code while
real multi-fault data does not exist yet. It is NOT evidence for the report -
propagation here is a simple additive model that we wrote, so CoRCA is bound to
look good on it. Real multi-fault results must come from Chaos Mesh runs.
"""
import networkx as nx
import numpy as np
import pandas as pd

from .topology import call_graph

FAULTS = ("cpu", "mem", "delay")


def generate(root_causes, faults=None, n_normal=300, n_anomal=300, seed=0,
             graph=None, fault_size=6.0, propagation=0.8):
    """Return (df, inject_time). Columns follow RCAEval naming: <svc>_cpu, _mem,
    _latency-90, _error, _workload."""
    rng = np.random.default_rng(seed)
    g = graph if graph is not None else call_graph()
    faults = faults or ["cpu"] * len(root_causes)
    n = n_normal + n_anomal
    after = np.arange(n) >= n_normal

    own_lat = {s: rng.normal(1.0, 0.1, n) for s in g.nodes}
    cpu = {s: rng.normal(20, 2, n) for s in g.nodes}
    mem = {s: rng.normal(200, 5, n) for s in g.nodes}

    for svc, f in zip(root_causes, faults):
        if f == "cpu":                 # CPU hog: own CPU up and own processing slower
            cpu[svc] = cpu[svc] + after * fault_size * 2
            own_lat[svc] = own_lat[svc] + after * fault_size * 0.1
        elif f == "mem":               # memory leak: mem ramps up, little latency effect
            mem[svc] = mem[svc] + after * np.linspace(0, fault_size * 10, n)
        elif f == "delay":             # network delay: only latency changes
            own_lat[svc] = own_lat[svc] + after * fault_size * 0.2

    # end-to-end latency: own + propagation * callees' latency (callees first)
    lat = {}
    for s in reversed(list(nx.topological_sort(g))):
        lat[s] = own_lat[s] + propagation * sum(lat[c] for c in g.successors(s)) / max(1, g.out_degree(s))

    cols = {"time": np.arange(n)}
    for s in g.nodes:
        cols[f"{s}_cpu"] = cpu[s]
        cols[f"{s}_mem"] = mem[s]
        cols[f"{s}_latency-90"] = lat[s] + rng.normal(0, 0.05, n)
        cols[f"{s}_error"] = rng.poisson(0.2, n).astype(float)
        cols[f"{s}_workload"] = rng.normal(50, 3, n)
    return pd.DataFrame(cols), n_normal


def drop_telemetry(df, rate: float, seed=0, keep_services=()):
    """Drop a repeatable fraction of metric columns to simulate missing monitoring streams."""
    if not 0 <= rate < 1:
        raise ValueError("rate must be in [0, 1)")
    rng = np.random.default_rng(seed)
    keep = tuple(f"{svc}_" for svc in keep_services)
    cols = [c for c in df.columns if c != "time" and not c.startswith(keep)]
    n = int(round(len(cols) * rate))
    return df.drop(columns=list(rng.choice(cols, n, replace=False))) if n else df.copy()


def baro_like_rank(df, inject_time):
    """BARO's ranking rule without needing RCAEval installed (used in tests/demos)."""
    from .base import anomaly_scores
    return list(anomaly_scores(df, inject_time).index)
