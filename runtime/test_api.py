import math
import pytest

from fastapi.testclient import TestClient

from corca.synthetic import generate
from runtime.api import Incident, calibration_thresholds, create_app
from corca.circa import FEATURE_POLICY
from corca.corca import STOPPING_POLICY


TOKEN = "test-token-that-is-long-enough"


def payload():
    return {"columns": ["time", "cartservice_cpu", "frontend_cpu"],
            "rows": [[i, (20 if i < 30 else 100) + (i % 3) * .1, 10 + (i % 3) * .1] for i in range(60)],
            "inject_time": 30, "tau": 6.0, "edges": [["frontend", "cartservice"]]}


def test_authentication_and_health():
    with TestClient(create_app(TOKEN)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/diagnose", json=payload()).status_code == 401
        assert client.get("/readyz", headers={"X-API-Key": TOKEN}).status_code == 200


def test_invalid_windows_and_payload_limits():
    headers = {"X-API-Key": TOKEN}
    with TestClient(create_app(TOKEN)) as client:
        data = payload()
        data["rows"][1][0] = data["rows"][0][0]
        assert client.post("/diagnose", json=data, headers=headers).status_code == 422
        data = payload()
        data["rows"][0].pop()
        assert client.post("/diagnose", json=data, headers=headers).status_code == 422
        assert client.post("/diagnose", content=b"x" * (4 * 1024 * 1024 + 1), headers=headers).status_code == 413


def test_insufficient_telemetry_and_missing_calibration():
    with TestClient(create_app(TOKEN)) as client:
        headers = {"X-API-Key": TOKEN}
        data = payload()
        data.pop("tau")
        assert client.post("/diagnose", json=data, headers=headers).status_code == 422
        data["tau"] = 6
        data["rows"] = [[row[0]] + [None] * (len(row) - 1) for row in data["rows"]]
        response = client.post("/diagnose", json=data, headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "insufficient_evidence"
        assert response.json()["root_causes"] == []


def test_real_baro_diagnosis_and_fractional_timeout():
    with TestClient(create_app(TOKEN, timeout=20)) as client:
        response = client.post("/diagnose", json=payload(), headers={"X-API-Key": TOKEN})
        assert response.status_code == 200, response.text
        result = response.json()
        assert "cartservice" in result["root_causes"]
        assert result["confidence_kind"] == "anomaly_score_ratio_not_probability"
        assert math.isfinite(result["seconds"])
    with TestClient(create_app(TOKEN, timeout=.001)) as client:
        result = client.post("/diagnose", json=payload(), headers={"X-API-Key": TOKEN}).json()
        assert result["status"] == "partial_timeout" and result["timed_out"]


def test_healthy_window_abstains_when_anomaly_gate_is_configured():
    data = {"columns": ["time", "cartservice_cpu"],
            "rows": [[i, 20] for i in range(30)], "inject_time": 15, "tau": 6, "anomaly_threshold": 6}
    with TestClient(create_app(TOKEN)) as client:
        result = client.post("/diagnose", json=data, headers={"X-API-Key": TOKEN}).json()
        assert result["status"] == "no_significant_anomaly" and not result["root_causes"]


@pytest.mark.parametrize("residual,timed_out,status", [
    (7, False, "partial_unexplained"), (6, False, "diagnosed"),
    (5, False, "diagnosed"), (7, True, "partial_timeout")])
def test_unexplained_evidence_is_not_reported_as_complete(monkeypatch, residual, timed_out, status):
    from corca.corca import CoRCAResult, Round
    from runtime import api
    result = CoRCAResult(root_causes=["cartservice"],
                         rounds=[Round("cartservice", 100, .9, [], residual)],
                         timed_out=timed_out, remaining_anomaly_score=residual,
                         remaining_service_scores={"frontend": residual})
    monkeypatch.setattr(api, "corca", lambda *args, **kwargs: result)
    with TestClient(create_app(TOKEN)) as client:
        response = client.post("/diagnose", json=payload(), headers={"X-API-Key": TOKEN})
        assert response.status_code == 200
        output = response.json()
        assert output["status"] == status
        assert output["root_causes"] == ["cartservice"]
        assert output["remaining_anomaly_score"] == residual
        assert output["unexplained_services"] == (["frontend"] if residual > 6 else [])


def test_circa_singular_input_returns_evidence_status_not_server_error():
    data = payload()
    data["method"] = "circa"
    data["rows"] = [[row[0], row[1], row[1] * 2] for row in data["rows"]]
    with TestClient(create_app(TOKEN)) as client:
        response = client.post("/diagnose", json=data, headers={"X-API-Key": TOKEN})
        assert response.status_code == 200
        assert response.json()["status"] == "insufficient_evidence"
        assert "singular" in response.json()["reason"]
        assert response.json()["root_causes"] == []


def test_real_circa_api_uses_observed_samples():
    import numpy as np
    rng = np.random.default_rng(4)
    values = rng.normal(10, 1, 60)
    values[30:] += 40
    data = {"columns": ["time", "cartservice_cpu", "a_workload", "b_workload"],
            "rows": [[i * 5, float(v), i, i] for i, v in enumerate(values)],
            "inject_time": 150, "tau": 6, "method": "circa", "edges": []}
    with TestClient(create_app(TOKEN)) as client:
        result = client.post("/diagnose", json=data, headers={"X-API-Key": TOKEN})
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "diagnosed"
        assert result.json()["root_causes"] == ["cartservice"]
        assert result.json()["feature_policy"] == FEATURE_POLICY
        assert result.json()["graph_excluded_metrics"] == ["a_workload", "b_workload"]


def test_circa_calibration_is_bound_to_feature_policy():
    calibration = {"methods": {"circa": {"tau": 6}}, "stopping_policy": STOPPING_POLICY}
    with pytest.raises(ValueError, match="recalibrate"):
        calibration_thresholds(calibration)
    calibration["feature_policies"] = {"circa": FEATURE_POLICY}
    assert calibration_thresholds(calibration) == {"circa": 6}
    calibration["stopping_policy"] = "old-policy"
    with pytest.raises(ValueError, match="stopping policy"):
        calibration_thresholds(calibration)
    calibration["stopping_policy"] = STOPPING_POLICY
    for invalid in (-1, float("inf"), True, "6"):
        calibration["methods"]["circa"]["tau"] = invalid
        with pytest.raises(ValueError, match="thresholds"):
            calibration_thresholds(calibration)


def test_invalid_capacity_is_rejected():
    for capacity in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="concurrency"):
            create_app(TOKEN, concurrency=capacity)


def test_capacity_rejection_recovers_slot(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from runtime import api
    started, release = threading.Event(), threading.Event()
    def diagnose(*args):
        started.set()
        assert release.wait(5)
        return {"status": "diagnosed"}
    monkeypatch.setattr(api, "diagnose", diagnose)
    with TestClient(create_app(TOKEN, concurrency=1)) as client, ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, "/diagnose", json=payload(), headers={"X-API-Key": TOKEN})
        try:
            assert started.wait(3)
            blocked = client.post("/diagnose", json=payload(), headers={"X-API-Key": TOKEN})
            assert blocked.status_code == 503 and blocked.headers["Retry-After"] == "5"
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 200
        assert client.post("/diagnose", json=payload(), headers={"X-API-Key": TOKEN}).status_code == 200
