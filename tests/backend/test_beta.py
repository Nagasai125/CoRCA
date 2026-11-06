"""Beta acceptance: real HTTP telemetry → workspace setup → worker → recovery."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from backend.admin import copy_database
from backend.api import Jobs, create_app
from tests.backend.test_api import wait_done

TOKEN = "beta-acceptance-token-long-enough"
AUTH = {"X-API-Key": TOKEN}


@pytest.fixture
def telemetry():
    incident = int(time.time()) - 300

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            start, end, step = (int(float(query[key][0])) for key in ("start", "end", "step"))
            body = {"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"service": service}, "values": [
                    [t, str((100 if service == "cartservice" and t >= incident else 20) + (t % 3) * .1)]
                    for t in range(start, end + 1, step)]}
                for service in ("cartservice", "frontend")]}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"prometheus_url": f"http://127.0.0.1:{server.server_port}", "queries": {"cpu": "cpu"}, "step": 5}, incident
    server.shutdown()
    server.server_close()
    thread.join()


def setup_workspace(client, source):
    connected = client.post("/v1/connections", headers=AUTH, json={"source": source})
    assert connected.status_code == 200, connected.text
    assert client.post("/v1/integrations/check", headers=AUTH).json()["status"] == "verified"
    discovery = client.post("/v1/services/discover", headers=AUTH).json()
    assert {s["name"] for s in discovery["services"]} == {"cartservice", "frontend"}
    saved = client.post("/v1/topology", headers=AUTH, json={"revision": discovery["revision"],
        "services": ["cartservice", "frontend"], "edges": [["frontend", "cartservice"]]})
    assert saved.status_code == 200, saved.text
    revision = saved.json()["revision"]
    sensitivity = client.post("/v1/sensitivity", headers=AUTH, json={"revision": revision, "tau": 6})
    assert sensitivity.status_code == 200, sensitivity.text
    return revision


def test_user_workflow_survives_backup_restore_without_calibration(tmp_path, telemetry):
    source, incident = telemetry
    db = tmp_path / "workspace.db"
    with TestClient(create_app(TOKEN, db_path=db)) as client:
        assert client.get("/readyz").status_code == 200
        assert client.post("/v1/sensitivity", json={"revision": "", "tau": 6}).status_code == 401
        revision = setup_workspace(client, source)
        assert client.get("/v1/integrations", headers=AUTH).json()["calibrated_methods"] == []
        queued = client.post("/v1/diagnoses", headers=AUTH, json={"inject_time": incident})
        assert queued.status_code == 202, queued.text
        job_id = queued.json()["id"]
        job = wait_done(client, job_id, AUTH)
        assert job["status"] == "done", job
        assert "cartservice" in job["result"]["root_causes"]
        assert job["result"]["threshold_mode"] == "manual"
        assert client.post(f"/v1/diagnoses/{job_id}/cancel", headers=AUTH).status_code == 409
        # A later setting does not rewrite the completed diagnosis's provenance.
        assert client.post("/v1/sensitivity", headers=AUTH, json={"revision": revision, "tau": 9}).status_code == 200
        assert client.get(f"/v1/diagnoses/{job_id}", headers=AUTH).json()["result"]["tau"] == 6
        healthy = client.post("/v1/diagnoses", headers=AUTH, json={"inject_time": incident - 600})
        healthy_job = wait_done(client, healthy.json()["id"], AUTH)
        assert healthy_job["result"]["status"] == "no_significant_anomaly"
        assert client.get("/v1/config", headers=AUTH).headers["cache-control"] == "no-store"
        copy_database(db, tmp_path / "backup.db")  # consistent snapshot while running
    copy_database(tmp_path / "backup.db", tmp_path / "restored.db")
    assert os.stat(tmp_path / "backup.db").st_mode & 0o777 == 0o600
    with TestClient(create_app(TOKEN, db_path=tmp_path / "restored.db")) as restored:
        assert restored.get("/v1/config", headers=AUTH).json()["methods"] == ["baro"]
        assert restored.get("/v1/integrations", headers=AUTH).json()["threshold_mode"] == "manual"
        assert restored.get(f"/v1/diagnoses/{job_id}", headers=AUTH).json() == job


def test_queue_bound_cancellation_and_alert_deduplication(tmp_path, telemetry):
    source, _ = telemetry
    db = tmp_path / "workspace.db"
    with TestClient(create_app(TOKEN, db_path=db, max_pending=1)) as client:
        revision = setup_workspace(client, source)
        hook = {"status": "firing", "groupKey": "beta-test", "alerts": [
            {"status": "firing", "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 120))}]}
        first = client.post("/v1/alerts", headers=AUTH, json=hook).json()
        assert first["accepted"]
        assert client.post("/v1/alerts", headers=AUTH, json=hook).json()["duplicate"]
        blocked = client.post("/v1/diagnoses", headers=AUTH, json={"inject_time": time.time()})
        assert blocked.status_code == 503 and blocked.headers["retry-after"] == "30"
        assert client.post(f"/v1/diagnoses/{first['id']}/cancel").status_code == 401
        cancelled = client.post(f"/v1/diagnoses/{first['id']}/cancel", headers=AUTH)
        assert cancelled.json()["status"] == "cancelled"
        assert client.post("/v1/diagnoses", headers=AUTH, json={"inject_time": time.time()}).status_code == 202
        assert client.post("/v1/sensitivity", headers=AUTH, json={"revision": "stale", "tau": 6}).status_code == 409
        for tau in (0, -1, 1001, "NaN"):
            assert client.post("/v1/sensitivity", headers=AUTH, json={"revision": revision, "tau": tau}).status_code == 422
    with pytest.raises(ValueError):
        create_app(TOKEN, max_pending=0)


def test_backup_refuses_overwrite_or_non_workspace(tmp_path):
    path = tmp_path / "workspace.db"
    Jobs(path)
    destination = tmp_path / "backup.db"
    copy_database(path, destination)
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        copy_database(path, destination)
    assert destination.read_bytes() == before
    with sqlite3.connect(tmp_path / "other.db") as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ValueError, match="workspace"):
        copy_database(tmp_path / "other.db", tmp_path / "bad.db")
    assert not (tmp_path / "bad.db").exists()


def test_direct_workspace_without_key(tmp_path, telemetry):
    source, incident = telemetry
    with TestClient(create_app(db_path=tmp_path / "open.db")) as client:
        page = client.get("/app")
        assert page.status_code == 200 and 'id="signin-form"' not in page.text
        assert client.get("/v1/access/session").json()["enabled"] is False
        assert client.get("/v1/config").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        # Every setup step and worker result is accessible without a key.
        connected = client.post("/v1/connections", json={"source": source}).json()
        topology = client.post("/v1/topology", json={"revision": connected["revision"],
            "services": ["cartservice", "frontend"], "edges": [["frontend", "cartservice"]]}).json()
        setup = {"revision": topology["revision"], "tau": 6}
        assert client.post("/v1/sensitivity", json=setup, headers={"Origin": "https://other.example"}).status_code == 403
        assert client.post("/v1/sensitivity", json=setup, headers={"Origin": "http://testserver"}).status_code == 200
        job = client.post("/v1/diagnoses", json={"inject_time": incident})
        assert job.status_code == 202
        result = wait_done(client, job.json()["id"], {})
        assert result["status"] == "done" and "cartservice" in result["result"]["root_causes"]


def test_saved_service_page_survives_restart_without_fetching_telemetry(tmp_path):
    database = tmp_path / "scope.db"
    configured = {"prometheus_url": "http://localhost:9090", "queries": {"cpu": "cpu"},
                  "services": ["frontend", "cartservice"], "edges": [["frontend", "cartservice"]]}

    def unavailable(*_):
        pytest.fail("Opening saved service scope must not depend on provider availability")

    with TestClient(create_app(db_path=database, fetch=unavailable)) as client:
        assert client.get("/v1/services").json()["services"] == []
        assert client.post("/v1/connections", json={"source": configured}).status_code == 200
        before = client.get("/v1/services").json()
        assert before["selected_services"] == ["frontend", "cartservice"]
        assert before["dependencies"] == [["frontend", "cartservice"]]
        assert before["topology_configured"] and before["checked_at"] is None
        assert all(row["status"] == "unverified" and not row["coverage"] for row in before["services"])
    with TestClient(create_app(db_path=database, fetch=unavailable)) as client:
        assert client.get("/v1/services").json() == before
    with TestClient(create_app(TOKEN, db_path=database, fetch=unavailable)) as client:
        assert client.get("/v1/services").status_code == 401


def test_discovery_finds_services_outside_saved_selection(tmp_path):
    import pandas as pd
    seen = []

    def fetch(configured, *_):
        seen.append(configured["services"])
        return pd.DataFrame({"time": [1, 2], "frontend_cpu": [2, 3], "newservice_cpu": [3, 4]})

    source = {"prometheus_url": "http://localhost:9090", "queries": {"cpu": "cpu"},
              "services": ["frontend"], "edges": []}
    with TestClient(create_app(db_path=tmp_path / "scope.db", fetch=fetch)) as client:
        assert client.post("/v1/connections", json={"source": source}).status_code == 200
        result = client.post("/v1/services/discover").json()
        assert seen == [[]]
        assert {row["name"] for row in result["services"]} == {"frontend", "newservice"}
        assert result["selected_services"] == ["frontend"]
