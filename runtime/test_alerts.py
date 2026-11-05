from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from runtime.api import create_app
from runtime.telemetry import validate_source

TOKEN = "alert-inbox-access-token"
AUTH = {"X-API-Key": TOKEN}


def source():
    return validate_source({"prometheus_url":"http://localhost:9090", "queries":{"cpu":"cpu"},
                            "services":["cart"], "edges":[], "step":5})


def hook(status="firing", identity="cart-latency", start=None):
    start = start or datetime.now(timezone.utc) + timedelta(minutes=5)
    return {"status":status, "groupKey":identity,
            "alerts":[{"status":status,"startsAt":start.isoformat(), "fingerprint":identity,
                       "labels":{"alertname":"CartLatency", "service":"cart", "severity":"critical"},
                       "annotations":{"summary":"Requests are timing out <script>unsafe()</script>", "description":"not retained"}}]}


def test_alerts_are_persisted_deduplicated_linked_and_resolved(tmp_path):
    database = tmp_path / "workspace.db"
    body = hook()
    with TestClient(create_app(TOKEN, {"baro":6}, source=source(), db_path=database)) as client:
        assert client.get("/v1/alerts").status_code == 401
        assert client.post("/v1/alerts/test").status_code == 401
        first = client.post("/v1/alerts", json=body, headers=AUTH)
        second = client.post("/v1/alerts", json=body, headers=AUTH)
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"] and second.json()["duplicate"]
        report = client.get("/v1/alerts", headers=AUTH).json()
        assert report["matching"] == 1 and report["delivery"]["firing"] == 1
        alert = report["alerts"][0]
        assert alert["job_id"] == first.json()["id"] and alert["diagnosis_status"] == "queued"
        assert alert["deliveries"] == 2 and "not retained" not in str(alert)
        resolved = {**body, "status":"resolved", "alerts":[{**body["alerts"][0], "status":"resolved", "endsAt":datetime.now(timezone.utc).isoformat()}]}
        assert client.post("/v1/alerts", json=resolved, headers=AUTH).json()["accepted"] is False
        assert client.get("/v1/alerts?status=firing", headers=AUTH).json()["matching"] == 0
        report = client.get("/v1/alerts?status=resolved&q=cart", headers=AUTH).json()
        assert report["matching"] == 1 and report["alerts"][0]["job_id"] == first.json()["id"]
        # A delayed retry cannot resurrect a resolved episode or enqueue more work.
        assert client.post("/v1/alerts", json=body, headers=AUTH).json()["accepted"] is False
        assert client.get("/v1/alerts?status=firing", headers=AUTH).json()["matching"] == 0
        assert len(client.get("/v1/diagnoses", headers=AUTH).json()) == 1
    with TestClient(create_app(TOKEN, db_path=database)) as client:
        report = client.get("/v1/alerts", headers=AUTH).json()
        assert report["alerts"][0]["status"] == "resolved"
        assert report["delivery"]["last_external_at"] is not None


def test_receiver_records_alerts_before_diagnosis_setup_and_local_test_is_distinct(tmp_path):
    database = tmp_path / "workspace.db"
    with TestClient(create_app(db_path=database)) as client:
        result = client.post("/v1/alerts", json=hook())
        assert result.status_code == 503
        report = client.get("/v1/alerts").json()
        assert not report["diagnoses_ready"] and report["receiver_ready"]
        assert report["delivery"]["firing"] == 1 and report["alerts"][0]["diagnosis_error"]
    with TestClient(create_app(db_path=tmp_path / "local-test.db")) as client:
        result = client.post("/v1/alerts/test").json()
        assert result["test_only"] and not result["external_delivery_verified"]
        report = client.get("/v1/alerts").json()
        assert report["delivery"]["last_external_at"] is None
        assert report["alerts"][0]["origin"] == "console_test" and report["alerts"][0]["job_id"] is None
        assert client.get("/v1/diagnoses").json() == []
        assert client.post("/v1/alerts/test", headers={"Origin":"https://untrusted.example"}).status_code == 403


def test_queue_pressure_keeps_receipts_and_duplicate_job_links(tmp_path):
    with TestClient(create_app(thresholds={"baro":6}, source=source(), db_path=tmp_path / "workspace.db", max_pending=1)) as client:
        body = hook()
        first = client.post("/v1/alerts", json=body).json()
        rejected = client.post("/v1/alerts", json=hook(identity="other-alert"))
        assert rejected.status_code == 503
        report = client.get("/v1/alerts").json()
        assert report["matching"] == 2
        assert any("queue is full" in (event["diagnosis_error"] or "") for event in report["alerts"])
        repeated = client.post("/v1/alerts", json=body).json()
        assert repeated["duplicate"] and repeated["id"] == first["id"]


def test_server_side_filters_cover_events_beyond_the_latest_page(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "workspace.db")) as client:
        for i in range(205):
            assert client.post("/v1/alerts", json=hook(status="resolved", identity=f"event-{i}")).status_code == 200
        all_events = client.get("/v1/alerts").json()
        assert all_events["matching"] == 205 and len(all_events["alerts"]) == 200
        assert client.get("/v1/alerts?status=resolved&q=cart").json()["matching"] == 205
        assert client.get("/v1/alerts?status=firing").json()["matching"] == 0
        assert client.get("/v1/alerts?q=not-found").json()["matching"] == 0


def test_invalid_payloads_do_not_create_events(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "workspace.db")) as client:
        body = hook();body["alerts"][0]["labels"]["alertname"] = "x" * 513
        assert client.post("/v1/alerts", json=body).status_code == 422
        assert client.get("/v1/alerts").json()["matching"] == 0
        assert client.post("/v1/alerts", json=hook(start=datetime.now(timezone.utc)+timedelta(days=1))).status_code == 422
        assert client.get("/v1/alerts").json()["matching"] == 0
