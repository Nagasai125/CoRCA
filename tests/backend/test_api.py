import math
import pytest

from fastapi.testclient import TestClient

from corca.synthetic import generate
from backend.api import Incident, calibration_thresholds, create_app
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
        home = client.get("/")
        assert home.status_code == 200 and "investigate the root causes" in home.text
        assert client.get("/app").status_code == 200
        # Bundled typefaces work offline and expose only the public asset directory.
        for name in ("roboto-flex", "google-sans-code"):
            font = client.get(f"/assets/fonts/{name}.woff")
            assert font.status_code == 200 and font.content.startswith(b"wOFF")
        assert client.get("/assets/fonts/missing.woff").status_code == 404
        assert client.get("/assets/fonts/%2e%2e/app.html").status_code == 404
        assert client.get("/v1/config").status_code == 401
        assert client.get("/v1/config", headers={"X-API-Key": TOKEN}).json() == {
            "methods": [], "prometheus": False, "alert_method": None}
        assert client.post("/diagnose", json=payload()).status_code == 401
        assert client.get("/readyz").status_code == 200  # onboarding must be reachable before configuration
        assert client.get("/openapi.json").status_code == 401
    with TestClient(create_app(TOKEN, {"baro": 6.0})) as client:
        assert client.get("/readyz").status_code == 200
        assert "/v1/diagnoses" in client.get("/openapi.json", headers={"Authorization": f"Bearer {TOKEN}"}).json()["paths"]


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


def test_explicit_anomaly_gate_is_not_reported_as_calibrated():
    data = {"columns":["time","cartservice_cpu"],"rows":[[i,20] for i in range(30)],
            "inject_time":15,"anomaly_threshold":6}
    with TestClient(create_app(TOKEN,thresholds={'baro':6})) as client:
        response = client.post('/diagnose',json=data,headers={'X-API-Key':TOKEN})
        assert response.status_code==200,response.text
        assert response.json()['threshold_mode']=='manual'


@pytest.mark.parametrize("residual,timed_out,status", [
    (7, False, "partial_unexplained"), (6, False, "diagnosed"),
    (5, False, "diagnosed"), (7, True, "partial_timeout")])
def test_unexplained_evidence_is_not_reported_as_complete(monkeypatch, residual, timed_out, status):
    from corca.corca import CoRCAResult, Round
    from backend import api
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
    from backend import api
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


# ---- Prometheus-backed jobs -------------------------------------------------------------
import time as _time

import pandas as pd
import requests

from backend.api import Jobs
from backend.prometheus import fetch_window, validate_source

T0 = 1_000_000
SOURCE = validate_source({"prometheus_url": "http://prometheus:9090", "queries": {"cpu": "q"},
                          "edges": [["frontend", "cartservice"]]})


def fake_fetch(source, start, end):
    rows = payload()["rows"]
    return pd.DataFrame([[T0 + r[0], *r[1:]] for r in rows], columns=payload()["columns"])


def wait_done(client, job_id, headers):
    for _ in range(60):
        job = client.get(f"/v1/diagnoses/{job_id}", headers=headers).json()
        if job["status"] in ("done", "failed"):
            return job
        _time.sleep(.5)
    raise AssertionError(f"job did not finish: {job}")


def test_window_job_pulls_metrics_and_keeps_result(tmp_path):
    headers = {"X-API-Key": TOKEN}
    app = create_app(TOKEN, {"baro": 6.0}, source=SOURCE, db_path=tmp_path / "jobs.db", fetch=fake_fetch)
    with TestClient(app) as client:
        response = client.post("/v1/diagnoses", json={"inject_time": T0 + 30, "normal_seconds": 60}, headers=headers)
        assert response.status_code == 202, response.text
        job = wait_done(client, response.json()["id"], headers)
        assert job["status"] == "done" and "cartservice" in job["result"]["root_causes"]
        assert [j["id"] for j in client.get("/v1/diagnoses", headers=headers).json()] == [job["id"]]
        assert client.get("/v1/config", headers=headers).json()["methods"] == ["baro"]
        assert client.post("/v1/diagnoses", json={"inject_time": T0, "method": "circa"},
                           headers=headers).status_code == 422  # circa not calibrated
        assert client.get("/v1/diagnoses/nope", headers=headers).status_code == 404


def test_alertmanager_webhook_deduplicates_and_uses_bearer_auth(tmp_path):
    app = create_app(TOKEN, {"baro": 6.0}, source=SOURCE, db_path=tmp_path / "jobs.db", fetch=fake_fetch)
    hook = {"version": "4", "status": "firing", "groupKey": "{}:{alertname=\"HighLatency\"}", "receiver": "corca",
            "alerts": [{"status": "firing", "labels": {"alertname": "HighLatency"},
                        "startsAt": "2026-10-27T13:00:00.123456789Z"}]}
    with TestClient(app) as client:
        assert client.post("/v1/alerts", json=hook, headers={"Authorization": "Bearer wrong"}).status_code == 401
        auth = {"Authorization": f"Bearer {TOKEN}"}
        first = client.post("/v1/alerts", json=hook, headers=auth).json()
        second = client.post("/v1/alerts", json=hook, headers=auth).json()
        assert not first["duplicate"] and second["duplicate"] and first["id"] == second["id"]
        assert first["inject_time"] == pd.Timestamp("2026-10-27T13:00:00Z").timestamp() - 60 + .123456
        resolved = dict(hook, status="resolved", alerts=[dict(hook["alerts"][0], status="resolved")])
        assert client.post("/v1/alerts", json=resolved, headers=auth).json()["accepted"] is False


def test_prometheus_outage_is_retried_then_failed(tmp_path):
    def down(*_):
        raise requests.ConnectionError("refused")
    jobs = Jobs(tmp_path / "jobs.db")
    from backend.api import MAX_ATTEMPTS, run_job
    job_id = jobs.add("baro", T0, 60, 60)[0]["id"]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        jobs.finish(job_id, "queued", ready_at=0)
        job = jobs.claim()
        assert job["attempts"] == attempt
        run_job(job, jobs, SOURCE, {"baro": 6.0}, 20, down)
    assert jobs.get(job_id)["status"] == "failed" and "Prometheus unavailable" in jobs.get(job_id)["error"]


def test_jobs_interrupted_by_a_restart_are_requeued(tmp_path):
    jobs = Jobs(tmp_path / "jobs.db")
    job_id = jobs.add("baro", T0, 60, 60)[0]["id"]
    assert jobs.claim()["id"] == job_id and jobs.get(job_id)["status"] == "running"
    reopened = Jobs(tmp_path / "jobs.db")
    assert reopened.get(job_id)["status"] == "running"
    with reopened.worker_owner():
        assert reopened.get(job_id)["status"] == "queued"


def test_fetch_window_maps_series_and_rejects_ambiguous_queries(monkeypatch):
    def series(*services, value="1"):
        return {"status": "success", "data": {"result": [
            {"metric": {"service": s}, "values": [[T0, value], [T0 + 5, "+Inf"]]} for s in services]}}
    replies = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: type("R", (), {
        "raise_for_status": lambda self: None, "json": lambda self: replies[0]})())
    source = validate_source({"prometheus_url": "http://p", "queries": {"cpu": "q"}, "services": ["cartservice"]})
    replies[:] = [series("cartservice", "loadgenerator")]
    frame = fetch_window(source, T0, T0 + 10)
    assert list(frame.columns) == ["time", "cartservice_cpu"]
    assert frame["cartservice_cpu"].tolist()[0] == 1 and frame["cartservice_cpu"].isna().sum() == 2  # +Inf and gap
    replies[:] = [series("cartservice", "cartservice")]
    with pytest.raises(ValueError, match="aggregate"):
        fetch_window(source, T0, T0 + 10)
    with pytest.raises(ValueError):
        validate_source({"prometheus_url": "ftp://p", "queries": {"cpu": "q"}})


def test_export_cases_rewrites_metrics_from_the_source(tmp_path, monkeypatch):
    from lab.export_cases import export
    case = tmp_path / "lab" / "cartservice_cpu" / "1"
    case.mkdir(parents=True)
    (case / "collection.json").write_text('{"status": "complete"}')
    (case / "ground_truth.json").write_text('{"root_causes": ["cartservice"]}')
    (case / "inject_time.txt").write_text(f"{T0 + 30}\n")
    pd.DataFrame({"time": [T0, T0 + 59], "x_trace": [0, 1]}).to_csv(case / "metrics.csv", index=False)
    windows = []
    def fetch(source, start, end):
        windows.append((start, end))
        return fake_fetch(source, start, end)
    monkeypatch.setattr('lab.export_cases.fetch_window', fetch)
    assert export(SOURCE, tmp_path / "lab", tmp_path / "live") == 1
    assert windows == [(T0, T0 + 59)]
    assert "cartservice_cpu" in pd.read_csv(tmp_path / "live/cartservice_cpu/1/metrics.csv").columns
    with pytest.raises(FileExistsError):
        export(SOURCE, tmp_path / "lab", tmp_path / "live")


def test_explained_lists_only_observed_services():
    data = payload()
    data["edges"] = [["gateway", "frontend"], ["frontend", "cartservice"]]  # gateway has no metrics
    with TestClient(create_app(TOKEN)) as client:
        result = client.post("/diagnose", json=data, headers={"X-API-Key": TOKEN}).json()
    assert result["rounds"][0]["service"] == "cartservice"
    assert result["rounds"][0]["explained"] == ["frontend"]


def test_integration_metadata_is_authenticated_and_redacted(tmp_path):
    source = dict(SOURCE, prometheus_url="https://user:secret@internal.example", queries={"cpu": "private_query"})
    with TestClient(create_app(TOKEN, {"baro": 6}, source=source, db_path=tmp_path / "jobs.db", fetch=fake_fetch)) as client:
        assert client.get("/v1/integrations").status_code == 401
        response = client.get("/v1/integrations", headers={"X-API-Key": TOKEN})
        assert response.json()["prometheus"]["metric_kinds"] == ["cpu"]
        assert response.json()["alertmanager"]["ready"] is True
        assert not any(value in response.text for value in ("secret", "internal.example", "private_query"))
        assert client.post("/v1/integrations/prometheus/check").status_code == 401
        check = client.post("/v1/integrations/prometheus/check", headers={"X-API-Key": TOKEN})
        assert check.status_code == 200 and check.json()["services"] == 2 and check.json()["metrics"] == 2


def test_integration_check_handles_missing_source_gaps_and_failure(tmp_path):
    headers = {"X-API-Key": TOKEN}
    with TestClient(create_app(TOKEN)) as client:
        assert client.get("/v1/integrations", headers=headers).json()["prometheus"]["configured"] is False
        assert client.post("/v1/integrations/prometheus/check", headers=headers).status_code == 409

    def no_data(*_):
        return pd.DataFrame({"time": [1, 2], "cartservice_cpu": [float("nan"), float("nan")]})

    def down(*_):
        raise requests.ConnectionError("secret URL must not leak")

    for fetch, status in ((no_data, 422), (down, 502), (fake_fetch, 200)):
        with TestClient(create_app(TOKEN, source=SOURCE, db_path=tmp_path / "checks.db", fetch=fetch)) as client:
            for _ in range(3):  # failures must release capacity for subsequent requests
                response = client.post("/v1/integrations/prometheus/check", headers=headers)
                assert response.status_code == status
                assert "secret URL" not in response.text
