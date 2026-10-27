"""Development-only architecture/RHT candidates; not registered in the API."""
from types import SimpleNamespace

import networkx as nx
import numpy as np
from RCAEval.classes.graph import MemoryGraph, Node
from RCAEval.graph_heads.rht import ANMRegressor, DAScorer, RHTScorer
from RCAEval.io.time_series import preprocess

from corca.circa import InsufficientEvidence
from corca.topology import metric_kind, service_of


STRUCTURAL_POLICIES = ("experimental-structural-rht-v1", "experimental-structural-rht-da-v1")
FAMILIES = ("traffic", "saturation", "latency", "errors")


class StrictANMRegressor(ANMRegressor):
    def score(self, train_x, test_x, train_y, test_y):
        if not len(train_x):
            return self._zscore(train_y, test_y)
        return self._score(train_x, test_x, train_y, test_y)


def family(metric):
    kind = metric_kind(metric)
    if kind in ("load", "workload"):
        return "traffic"
    if kind.startswith(("cpu", "mem", "disk", "socket")):
        return "saturation"
    if kind.startswith(("latency", "lat_")):
        return "latency"
    if kind in ("error", "log-error"):
        return "errors"
    raise InsufficientEvidence(f"structural mapping has no declared metric family for {metric}")


def structural_graph(names, calls):
    if not nx.is_directed_acyclic_graph(calls):
        raise InsufficientEvidence("structural candidate requires an acyclic call graph")
    services = sorted(set(calls) | {service_of(c) for c in names})
    skeleton, graph = nx.DiGraph(), nx.DiGraph()
    own = {}
    for name in names:
        node = Node(*name.split("_", 1))
        graph.add_node(node)
        own.setdefault((service_of(name), family(name)), set()).add(node)
    for service in services:
        skeleton.add_nodes_from((service, f) for f in FAMILIES)
        for parent, children in (("traffic", FAMILIES[1:]),
                                 ("saturation", FAMILIES[2:]), ("latency", ("errors",))):
            skeleton.add_edges_from(((service, parent), (service, child)) for child in children)
    for caller, callee in sorted(calls.edges):
        skeleton.add_edge((caller, "traffic"), (callee, "traffic"))
        for f in ("latency", "errors"):
            skeleton.add_edge((callee, f), (caller, f))
    proxies = {}
    for meta in nx.lexicographical_topological_sort(skeleton):
        parents = set().union(*(proxies[p] for p in skeleton.predecessors(meta)))
        metrics = own.get(meta, set())
        graph.add_edges_from((p, c) for p in parents for c in metrics)
        carried_errors = (set().union(*(proxies[p] for p in skeleton.predecessors(meta) if p[1] == "errors"))
                          if meta[1] == "errors" else set())
        proxies[meta] = (metrics | carried_errors) if metrics or carried_errors else parents
    if not nx.is_directed_acyclic_graph(graph):
        raise InsufficientEvidence("metric mapping introduced a causal cycle")
    return MemoryGraph(graph)


def rank_structural(df, inject_time, dataset, calls, *, adjustment=False, scorer=None):
    timestamps = df["time"].to_numpy(float)
    if (not np.isfinite(inject_time) or not np.isfinite(timestamps).all()
            or np.any(np.diff(timestamps) <= 0)):
        raise ValueError("structural timestamps must be finite and strictly increasing")
    data = preprocess(df.drop(columns=[c for c in df if c.endswith("_workload")]).copy(),
                      dataset=dataset, dk_select_useful=False).drop(columns="time", errors="ignore")
    normal = int((timestamps < inject_time).sum())
    anomalous = len(df) - normal
    if not len(data.columns) or min(normal, anomalous) < 10 or not np.isfinite(data.to_numpy(float)).all():
        raise InsufficientEvidence("structural RHT requires finite varying metrics and 10 observed samples per window")
    graph = structural_graph(data.columns, calls)
    max_parents = max(len(graph.parents(node)) for node in graph.nodes)
    if normal <= max_parents + 2:
        raise InsufficientEvidence(f"structural RHT needs more than {max_parents + 2} healthy samples")
    by_name = {f"{node.entity}_{node.metric}": node for node in graph.nodes}
    series = {by_name[c]: data[c].to_numpy(float) for c in sorted(data.columns)}
    window = SimpleNamespace(train_window=normal, test_window=anomalous, load_data=lambda graph, current: series)
    scorer = RHTScorer(regressor=StrictANMRegressor()) if scorer is None else scorer
    scores = scorer.score(graph=graph, data=window, current=float(timestamps[-1]))
    if adjustment:
        scores = DAScorer(threshold=3).score(graph, window, float(timestamps[-1]), scores=scores)
    if any(not np.isfinite(score.score) for score in scores.values()):
        raise InsufficientEvidence("structural RHT produced non-finite scores")
    for detail in getattr(scorer, "details", []):
        score = scores[by_name[detail["metric"]]]
        detail.update(adjusted_score=float(score.score), child_score=float(score.get("child_score", 0)))
    ranked = sorted(scores, key=lambda node: (node.entity, node.metric))
    ranked.sort(key=lambda node: scores[node].key, reverse=True)
    return [f"{node.entity}_{node.metric}" for node in ranked]
