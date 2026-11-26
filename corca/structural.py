"""Linear structural repair estimates, conditional on a supplied dependency DAG."""
import networkx as nx
import numpy as np
from sklearn.linear_model import Ridge

from .topology import metric_kind, service_of

MODEL_POLICY = "normal-window-linear-scm-v1"


def repair_effects(frame, onset, edges, roots):
    base = {"policy": MODEL_POLICY, "kind": "model_based_intervention",
            "assumptions": ["Correct acyclic dependency direction", "No unmeasured common causes",
                            "Stable linear mechanisms between normal and incident windows"],
            "note": "Hypothetical repair estimates; confirm effects with controlled interventions."}
    normal = frame["time"].to_numpy() < onset
    if normal.sum() < 30 or (~normal).sum() < 10:
        return {**base, "available": False, "reason": "At least 30 normal and 10 incident samples are required"}
    preferred = ("trace-latency-90", "latency-90", "trace-error", "error", "log-error", "cpu", "mem")
    columns = {}
    for column in frame:
        if column == "time":
            continue
        service, kind = service_of(column), metric_kind(column)
        priority = preferred.index(kind) if kind in preferred else len(preferred)
        if service not in columns or priority < columns[service][0]:
            columns[service] = (priority, column)
    selected = {service: value[1] for service, value in columns.items()}
    graph = nx.DiGraph()
    graph.add_nodes_from(selected)
    # Callee changes can affect callers, reversing the call-graph direction.
    graph.add_edges_from((b, a) for a, b in edges if a in selected and b in selected and a != b)
    if not nx.is_directed_acyclic_graph(graph):
        return {**base, "available": False, "reason": "Observed dependencies contain a cycle; this model requires a DAG"}
    if not graph.edges:
        return {**base, "available": False, "reason": "No observed propagation edges support a repair model"}

    values, means, scales, observed, equations, quality = {}, {}, {}, {}, {}, []
    for service, column in selected.items():
        raw = frame[column].to_numpy(float)
        if not np.isfinite(raw).all():
            return {**base, "available": False, "reason": "Repair estimates require prepared finite telemetry"}
        means[service] = float(np.mean(raw[normal]))
        scales[service] = max(float(np.std(raw[normal])), .01 * abs(means[service]), 1e-9)
        values[service] = (raw - means[service]) / scales[service]
        observed[service] = float(np.mean(values[service][~normal]))
    ordered = list(nx.topological_sort(graph))
    for service in ordered:
        parents = sorted(graph.predecessors(service))
        y = values[service][normal]
        if not parents:
            equations[service] = (parents, 0., np.array([]))
            continue
        x = np.column_stack([values[p][normal] for p in parents])
        split = int(len(y) * .7)
        if split < max(20, 3 * (len(parents) + 1)):
            return {**base, "available": False, "reason": "Too few normal samples for the observed parent count"}
        if np.std(y[:split]) < 1e-8 or any(np.std(x[:split, j]) < 1e-8 for j in range(len(parents))):
            return {**base, "available": False, "reason": "Normal telemetry lacks variation to identify dependency effects"}
        validation = Ridge(alpha=1.).fit(x[:split], y[:split])
        mse = float(np.mean((validation.predict(x[split:]) - y[split:]) ** 2))
        baseline = float(np.mean((y[split:] - y[:split].mean()) ** 2))
        quality.append({"service": service, "validation_samples": len(y) - split,
                        "normalized_rmse": float(np.sqrt(mse)), "baseline_rmse": float(np.sqrt(baseline))})
        if mse > baseline:
            return {**base, "available": False, "reason": "Dependency model does not outperform a constant normal baseline",
                    "validation": quality}
        model = Ridge(alpha=1.).fit(x, y)
        equations[service] = (parents, float(model.intercept_), model.coef_)
    noise = {s: observed[s] - intercept - sum(float(b) * observed[p] for p, b in zip(parents, coef))
             for s, (parents, intercept, coef) in equations.items()}
    repairs = []
    for root in roots:
        if root not in observed:
            continue
        repaired = dict(observed)
        repaired[root] = 0.  # do(response_root = normal mean), retaining other disturbances.
        for service in ordered:
            if service == root:
                continue
            parents, intercept, coef = equations[service]
            repaired[service] = intercept + noise[service] + sum(float(b) * repaired[p] for p, b in zip(parents, coef))
        affected = {root} | nx.descendants(graph, root)
        repairs.append({"service": root, "effects": [
            {"service": s, "metric": metric_kind(selected[s]),
             "observed": observed[s] * scales[s] + means[s],
             "after_repair": repaired[s] * scales[s] + means[s],
             "estimated_reduction": (observed[s] - repaired[s]) * scales[s]}
            for s in sorted(affected)]})
    return {**base, "available": bool(repairs), "normal_samples": int(normal.sum()),
            "validation": quality, "repairs": repairs}

