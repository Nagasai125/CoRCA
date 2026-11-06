"""Worker ownership, restart recovery, and readiness on isolated workspaces."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

from fastapi.testclient import TestClient
import pytest

from backend import api
from backend.api import Jobs, create_app


def test_opening_workspace_does_not_requeue_active_work(tmp_path):
    path = tmp_path / "workspace.db"
    jobs = Jobs(path)
    job = jobs.add("baro", time.time() - 1000, 300, 120)[0]
    assert jobs.claim()["id"] == job["id"]
    assert Jobs(path).get(job["id"])["status"] == "running"


def test_only_worker_owner_can_recover_jobs_and_restart_releases_ownership(tmp_path):
    path = tmp_path / "workspace.db"
    jobs = Jobs(path)
    job = jobs.add("baro", time.time() + 1800, 300, 120)[0]
    with TestClient(create_app(db_path=path)) as first:
        with jobs._db() as db:
            db.execute("UPDATE jobs SET status='running', attempts=1 WHERE id=?", (job["id"],))
        # Even constructing another application must not alter the active job.
        other = create_app(db_path=path)
        assert jobs.get(job["id"])["status"] == "running"
        with pytest.raises(RuntimeError, match="owns this workspace"):
            with TestClient(other):
                pytest.fail("A second worker must not start")
        assert first.get("/readyz").status_code == 200
        assert jobs.get(job["id"])["status"] == "running"
    with TestClient(create_app(db_path=path)) as restarted:
        recovered = restarted.get(f"/v1/diagnoses/{job['id']}").json()
        assert recovered["status"] == "queued" and recovered["attempts"] == 1
        assert path.with_name(path.name + ".worker.lock").stat().st_mode & 0o777 == 0o600


def test_readiness_reports_storage_failure_without_breaking_liveness(tmp_path, monkeypatch):
    with TestClient(create_app(db_path=tmp_path / "workspace.db")) as client:
        def unavailable(*args):
            raise sqlite3.OperationalError("simulated storage outage")
        monkeypatch.setattr(Jobs, "recent", unavailable)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"detail": "Workspace storage is unavailable"}
        assert client.get("/healthz").status_code == 200


def test_readiness_reports_a_stopped_worker(tmp_path, monkeypatch):
    finished = threading.Event()
    def stopped(*args):
        finished.set()
    monkeypatch.setattr(api, "work", stopped)
    with TestClient(create_app(db_path=tmp_path / "workspace.db")) as client:
        assert finished.wait(2)
        deadline = time.monotonic() + 2
        while (response := client.get("/readyz")).status_code == 200 and time.monotonic() < deadline:
            time.sleep(.01)
        assert response.status_code == 503
        assert response.json() == {"detail": "Diagnosis worker is unavailable"}
        assert client.get("/healthz").status_code == 200


def test_shutdown_retains_ownership_until_worker_finishes_and_lifespan_can_restart(tmp_path, monkeypatch):
    path = tmp_path / "workspace.db"
    draining, release = threading.Event(), threading.Event()
    starts = []
    def worker(jobs, source, thresholds, timeout, slots, fetch, stop):
        starts.append(stop.is_set())
        stop.wait()
        draining.set()
        assert release.wait(5)
    monkeypatch.setattr(api, "work", worker)
    app = create_app(db_path=path)
    client = TestClient(app)
    client.__enter__()
    with ThreadPoolExecutor(max_workers=1) as pool:
        closing = pool.submit(client.__exit__, None, None, None)
        try:
            assert draining.wait(2)
            assert not closing.done()
            with pytest.raises(RuntimeError, match="owns this workspace"):
                with TestClient(create_app(db_path=path)):
                    pytest.fail("Ownership was released before the worker stopped")
        finally:
            release.set()
        closing.result(timeout=3)
    with TestClient(app) as restarted:
        assert restarted.get("/readyz").status_code == 200
    assert starts == [False, False]
