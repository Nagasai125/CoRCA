"""Run with:  python -m pytest -q tests"""
from corca import corca, set_prf, top_k_hit
from corca.synthetic import baro_like_rank, drop_telemetry, generate
from corca.topology import affected_by, call_graph, service_of


def slow_after_first(d, ti):
    import time
    if "currencyservice_cpu" not in d.columns or "adservice_cpu" not in d.columns:
        time.sleep(5)
    return baro_like_rank(d, ti)


def test_topology():
    g = call_graph()
    assert affected_by("currencyservice", g) == {"currencyservice", "checkoutservice",
                                                  "frontend", "frontend-external"}
    assert service_of("redis-cart_cpu") == "redis"
    assert service_of("cartservice_latency-90") == "cartservice"


def test_metrics():
    assert set_prf(["a", "b"], ["a", "c"]) == (0.5, 0.5, 0.5)
    assert top_k_hit(["x", "a", "b"], ["a", "b"], 2) == 0.5


def test_single_fault_returns_one_cause():
    df, t = generate(["cartservice"], ["cpu"], seed=1)
    res = corca(df, t, rank_fn=baro_like_rank, tau=6.0)
    assert res.root_causes == ["cartservice"]
    assert 0.0 <= res.rounds[0].confidence <= 1.0


def test_two_independent_faults_found():
    # separate branches: neither is a caller of the other
    df, t = generate(["currencyservice", "adservice"], ["cpu", "cpu"], seed=2)
    res = corca(df, t, rank_fn=baro_like_rank, tau=6.0)
    assert set(res.root_causes) == {"currencyservice", "adservice"}
    assert res.remaining_anomaly_score <= 6


def test_round_limit_exposes_unexplained_evidence():
    df, t = generate(["currencyservice", "adservice"], ["cpu", "cpu"], seed=2)
    res = corca(df, t, rank_fn=baro_like_rank, tau=6, max_rounds=1)
    assert len(res.root_causes) == 1 and not res.timed_out
    assert res.remaining_anomaly_score == res.rounds[-1].residual > 6
    assert res.remaining_anomaly_score == max(res.remaining_service_scores.values())
    assert not set(res.root_causes) & res.remaining_service_scores.keys()
    assert ({"currencyservice", "adservice"} - set(res.root_causes)) <= res.remaining_service_scores.keys()


def test_same_path_needs_keep_resource():
    # checkoutservice calls currencyservice: explaining currency would hide checkout's
    # latency, but its own CPU spike should survive when keep_resource=True
    df, t = generate(["currencyservice", "checkoutservice"], ["cpu", "cpu"], seed=3)
    keep = corca(df, t, rank_fn=baro_like_rank, tau=6.0, keep_resource=True)
    assert set(keep.root_causes) == {"currencyservice", "checkoutservice"}


def test_round_timeout_keeps_found_causes():
    df, t = generate(["currencyservice", "adservice"], ["cpu", "cpu"], seed=2)
    res = corca(df, t, rank_fn=slow_after_first, round_timeout=2,
                initial_rank=baro_like_rank(df, t))
    assert res.timed_out and len(res.root_causes) == 1


def test_missing_telemetry_still_runs_when_root_metrics_remain():
    df, t = generate(["cartservice"], ["cpu"], seed=4)
    partial = drop_telemetry(df, 0.35, seed=4, keep_services=["cartservice"])
    res = corca(partial, t, rank_fn=baro_like_rank, tau=6.0)
    assert res.root_causes == ["cartservice"]
