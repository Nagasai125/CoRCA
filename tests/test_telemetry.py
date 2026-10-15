import pytest

from lab.telemetry import trace_features


def test_trace_topology_uses_normal_window_and_preserves_missing_buckets():
    def resource(service, spans):
        return {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                "scopeSpans": [{"spans": spans}]}

    def span(start, target, error=False):
        return {"kind": 3, "startTimeUnixNano": str(start * 10**9),
                "endTimeUnixNano": str(start * 10**9 + 100_000_000),
                "attributes": [{"key": "rpc.service", "value": {"stringValue": "hipstershop." + target}}],
                "status": {"code": 2 if error else 1}}

    records = [{"resourceSpans": [resource("frontend", [span(1, "CartService"), span(11, "CurrencyService", True)])]}]
    features, edges = trace_features(records, [0, 5, 10], normal_end=10)
    assert edges == [("frontend", "cartservice")]
    assert features.loc[0, "cartservice_latency-90"] == pytest.approx(100)
    assert features.loc[5, "cartservice_workload"] == 0
    assert features.loc[5, "cartservice_latency-90"] != features.loc[5, "cartservice_latency-90"]
    assert features.loc[10, "currencyservice_error"] == 1
