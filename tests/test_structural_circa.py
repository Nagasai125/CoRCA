import networkx as nx
import numpy as np
import pandas as pd
import pytest

from corca.circa import InsufficientEvidence
from scripts.structural_circa import StrictANMRegressor, rank_structural, structural_graph


def edges(graph):
    return {(f"{a.entity}_{a.metric}", f"{b.entity}_{b.metric}") for a, b in graph.edges}


def test_graph_direction_missing_meta_propagation_and_isolated_nodes():
    names = ["a_cpu", "a_mem", "a_latency", "a_error", "b_cpu", "b_error", "isolated_mem"]
    graph = structural_graph(names, nx.DiGraph([("a", "b")]))
    links = edges(graph)
    assert ("a_cpu", "a_latency") in links and ("a_mem", "a_latency") in links
    assert ("b_cpu", "a_latency") in links  # missing b latency inherits resource parents
    assert ("b_error", "a_error") in links
    assert ("a_cpu", "b_cpu") not in links and ("a_cpu", "a_mem") not in links
    assert len(graph.nodes) == len(names)


def test_missing_error_service_carries_evidence_to_callers():
    graph = structural_graph(["c_error", "a_error"], nx.DiGraph([("a", "b"), ("b", "c")]))
    assert ("c_error", "a_error") in edges(graph)


def test_redis_alias_maps_to_architecture_without_renaming_metric():
    graph = structural_graph(["redis-cart_cpu", "cartservice_latency"], nx.DiGraph([("cartservice", "redis")]))
    assert ("redis-cart_cpu", "cartservice_latency") in edges(graph)


def test_graph_rejects_cycles_and_unknown_metric_families():
    with pytest.raises(InsufficientEvidence, match="acyclic"):
        structural_graph(["a_cpu"], nx.DiGraph([("a", "b"), ("b", "a")]))
    with pytest.raises(InsufficientEvidence, match="no declared metric family"):
        structural_graph(["a_unknown"], nx.DiGraph())


def test_traffic_and_latency_have_opposite_cross_service_directions():
    graph = structural_graph(["a_load", "b_load", "a_cpu", "b_cpu", "a_latency", "b_latency"],
                             nx.DiGraph([("a", "b")]))
    links = edges(graph)
    assert ("a_load", "b_load") in links and ("b_load", "a_load") not in links
    assert ("b_latency", "a_latency") in links and ("a_latency", "b_latency") not in links


def test_per_parent_sample_gate_and_timestamp_validation():
    rng = np.random.default_rng(12)
    df = pd.DataFrame({f"a{i}_cpu": rng.normal(10, 1, 24) for i in range(15)})
    df.insert(0, "time", np.arange(24))
    assert len(rank_structural(df, 12, "re2-ob", nx.DiGraph())) == 15
    df.columns = ["time"] + [f"a_cpu{i}" for i in range(15)]
    df["a_latency"] = rng.normal(10, 1, 24)
    with pytest.raises(InsufficientEvidence, match="healthy samples"):
        rank_structural(df, 12, "re2-ob", nx.DiGraph())
    df.loc[1, "time"] = 0
    with pytest.raises(ValueError, match="strictly increasing"):
        rank_structural(df, 12, "re2-ob", nx.DiGraph())


@pytest.mark.parametrize("adjustment", [False, True])
def test_real_rht_keeps_observed_windows_and_input(adjustment):
    rng = np.random.default_rng(12)
    df = pd.DataFrame({"time": np.arange(60) * 5, "root_cpu": rng.normal(10, 1, 60),
                       "other_mem": rng.normal(10, 1, 60), "a_workload": np.arange(60)})
    df.loc[30:, "root_cpu"] += 40
    original = df.copy(deep=True)
    ranked = rank_structural(df, 150, "re2-ob", nx.DiGraph(), adjustment=adjustment)
    assert ranked[0] == "root_cpu" and "a_workload" not in ranked
    pd.testing.assert_frame_equal(df, original)


def test_strict_regressor_does_not_use_library_error_fallback():
    with pytest.raises(ValueError):
        StrictANMRegressor().score(np.ones((30, 1)), np.ones((30, 2)), np.arange(30), np.arange(30))
