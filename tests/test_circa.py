import numpy as np
import pandas as pd
import pytest

from corca.base import rank_metrics
from corca.circa import InsufficientEvidence
from corca.execution import call_with_timeout


def insufficient_worker():
    raise InsufficientEvidence("more observed samples required")


def window(metrics=1, normal=30, anomalous=30):
    rng = np.random.default_rng(12)
    df = pd.DataFrame({f"service{i}_cpu": rng.normal(20, 1, normal + anomalous) for i in range(metrics)})
    df.loc[normal:, "service0_cpu"] += 40
    df.insert(0, "time", np.r_[np.arange(normal) * 5, np.arange(anomalous) * 5 + normal * 5 + 30])
    return df, normal * 5


def test_real_circa_ranks_isolated_metric_and_does_not_mutate_input():
    df, t = window()
    original = df.copy(deep=True)
    assert rank_metrics("circa", df, t) == ["service0_cpu"]
    pd.testing.assert_frame_equal(df, original)


def test_circa_uses_actual_windows_and_causal_edge_direction(monkeypatch):
    from RCAEval.graph_construction import pc
    from RCAEval.graph_heads import rht
    from types import SimpleNamespace

    df, t = window(metrics=2)
    monkeypatch.setattr(pc, "pc_default", lambda data: np.array([[0, -1], [1, 0]]))

    def score(self, graph, data, current):
        assert data.train_window == 30 and data.test_window == 30
        assert current == df.time.iloc[-1]
        series = data.load_data(graph, current)
        by_name = {f"{node.entity}_{node.metric}": node for node in series}
        assert graph.parents(by_name["service1_cpu"]) == {by_name["service0_cpu"]}
        for name, node in by_name.items():
            np.testing.assert_array_equal(series[node], df[name].to_numpy())
            assert len(series[node]) == 60  # not resampled/padded across the 30s gap
        return {node: SimpleNamespace(score=float(index)) for index, node in enumerate(series)}

    monkeypatch.setattr(rht.RHTScorer, "score", score)
    assert set(rank_metrics("circa", df, t)) == {"service0_cpu", "service1_cpu"}


def test_short_normal_window_is_rejected_before_pc(monkeypatch):
    from RCAEval.graph_construction import pc
    monkeypatch.setattr(pc, "pc_default", lambda data: pytest.fail("PC must not run"))
    df, t = window(metrics=9, normal=10)
    with pytest.raises(InsufficientEvidence, match="10 normal samples for 9 varying metrics"):
        rank_metrics("circa", df, t)


def test_singular_streams_are_rejected_without_jitter_or_metric_deletion():
    df, t = window(metrics=2)
    df["service1_cpu"] = df["service0_cpu"] * 2
    original = df.copy()
    with pytest.raises(InsufficientEvidence, match="singular.*service0_cpu.*service1_cpu"):
        rank_metrics("circa", df, t)
    pd.testing.assert_frame_equal(df, original)


def test_worker_preserves_insufficient_evidence_type():
    with pytest.raises(InsufficientEvidence, match="more observed samples"):
        call_with_timeout(insufficient_worker, 5)


def test_original_graph_learning_error_is_not_swallowed(monkeypatch):
    from RCAEval.graph_construction import pc
    def fail(data):
        raise RuntimeError("original graph error")
    monkeypatch.setattr(pc, "pc_default", fail)
    df, t = window(metrics=2)
    with pytest.raises(RuntimeError, match="original graph error"):
        rank_metrics("circa", df, t)


def test_invalid_timestamp_order_is_rejected():
    df, t = window()
    df.loc[1, "time"] = df.loc[0, "time"]
    with pytest.raises(ValueError, match="strictly increasing"):
        rank_metrics("circa", df, t)


def test_workload_policy_is_fixed_and_preserves_raw_input():
    df, t = window()
    for service in ("service0", "service1", "service2"):
        df[service + "_workload"] = np.arange(len(df))
    original = df.copy(deep=True)
    assert rank_metrics("circa", df, t) == ["service0_cpu"]
    pd.testing.assert_frame_equal(df, original)


def test_workload_only_window_is_explicitly_rejected():
    df, t = window()
    df = df.rename(columns={"service0_cpu": "service0_workload"})
    with pytest.raises(InsufficientEvidence, match="no graph metrics"):
        rank_metrics("circa", df, t)


def test_baro_keeps_workload_metrics(monkeypatch):
    from corca import base
    df, t = window()
    df["service0_workload"] = np.arange(len(df))
    def baro(frame, *args, **kwargs):
        assert "service0_workload" in frame
        return {"ranks": ["service0_workload", "service0_cpu"]}
    monkeypatch.setattr(base, "_rcaeval", lambda name: baro)
    assert rank_metrics("baro", df, t)[0] == "service0_workload"
