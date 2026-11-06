"""Provider contracts and the connection → discovery → calibration → diagnosis workflow."""
import json
import math
import sqlite3
import time

import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient

from corca.corca import STOPPING_POLICY
from backend.api import Jobs, create_app, run_job
from backend.telemetry import discover_services, fetch_window, validate_source

TOKEN = "test-connection-key-long-enough"
HEADERS = {"X-API-Key": TOKEN}
SECRET = "not-for-metadata-or-source-files"
START = 1_800_000_000


def source(provider="prometheus", **overrides):
    data = {"provider": provider, "queries": {"cpu": "q"}, "service_label": "service", "step": 60}
    if provider in ("prometheus", "grafana"):
        data["prometheus_url"] = "http://localhost:9090" if provider == "prometheus" else "https://prometheus.example.grafana.net/api/prom"
    else:
        data["url"] = {"datadog": "https://api.datadoghq.com", "dynatrace": "https://tenant.live.dynatrace.com", "newrelic": "https://api.newrelic.com/graphql"}[provider]
    if provider == "datadog":
        data.update(api_key_env="CORCA_TELEMETRY_DD_KEY", app_key_env="CORCA_TELEMETRY_DD_APP")
    elif provider != "prometheus":
        data["token_env"] = "CORCA_TELEMETRY_TOKEN"
    if provider == "grafana":
        data["username"] = "12345"
    if provider == "newrelic":
        data.update(account_id=123, queries={"cpu": "SELECT average(cpu) AS value FROM Metric FACET service"})
    data.update(overrides)
    return validate_source(data)


class Reply:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


@pytest.mark.parametrize("provider", ["datadog", "dynatrace", "newrelic"])
def test_provider_adapters_preserve_observed_points_and_auth(monkeypatch, provider):
    for name in ("CORCA_TELEMETRY_DD_KEY", "CORCA_TELEMETRY_DD_APP", "CORCA_TELEMETRY_TOKEN"):
        monkeypatch.setenv(name, SECRET)
    bodies = {
        "datadog": {"status": "ok", "series": [{"tag_set": ["service:payments"], "pointlist": [[START * 1000, 2], [(START+60)*1000, None], [(START+120)*1000, 4]]}]},
        "dynatrace": {"result": [{"data": [{"dimensionMap": {"service": "payments"}, "timestamps": [START*1000, (START+60)*1000, (START+120)*1000], "values": [2, None, 4]}]}]},
        "newrelic": {"data": {"actor": {"account": {"nrql": {"results": [{"facet": ["payments"], "beginTimeSeconds": START, "value": 2}, {"facet": ["payments"], "beginTimeSeconds": START+60, "value": None}, {"facet": ["payments"], "beginTimeSeconds": START+120, "value": 4}]}}}}},
    }
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return Reply(bodies[provider])

    monkeypatch.setattr(requests, "request", request)
    frame = fetch_window(source(provider), START, START+120)
    assert frame["~payments_cpu"].iloc[[0, 2]].tolist() == [2, 4]
    assert math.isnan(frame["~payments_cpu"].iloc[1])
    assert frame["time"].tolist() == [START, START+60, START+120]
    args, options = calls[0]
    assert not options["allow_redirects"] and options["timeout"] == 30
    assert SECRET in options["headers"].values() or "Api-Token " + SECRET in options["headers"].values()
    assert SECRET not in args[1] and SECRET not in json.dumps(options.get("params", {}))
    if provider == "newrelic":
        query = options["json"]["query"]
        assert "SINCE" in query and "TIMESERIES 1 minute" in query and "account(id: 123)" in query


def test_grafana_uses_prometheus_query_api_and_basic_auth(monkeypatch):
    monkeypatch.setenv("CORCA_TELEMETRY_TOKEN", SECRET)
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Reply({"status": "success", "data": {"result": [{"metric": {"service": "payments"}, "values": [[START, "2"], [START+60, "3"]]}]}})

    monkeypatch.setattr(requests, "get", get)
    frame = fetch_window(source("grafana"), START, START+60)
    assert frame["payments_cpu"].tolist() == [2, 3]
    assert calls[0][0].endswith("/api/prom/api/v1/query_range")
    assert calls[0][1]["auth"] == ("12345", SECRET)
    assert calls[0][1]["allow_redirects"] is False


def test_sources_reject_plaintext_credentials_and_invalid_provider_settings():
    base = source()
    for change in ({"password": SECRET}, {"prometheus_url": "http://user:secret@localhost"},
                   {"prometheus_url": "http://localhost?token=secret"}, {"prometheus_url": 123},
                   {"token_env": "a secret value"}, {"service_label": {}}, {"alert_lead_seconds": float("nan")}):
        with pytest.raises(ValueError):
            validate_source({**base, **change})
    with pytest.raises(ValueError, match="60-second"):
        source("datadog", step=5)
    with pytest.raises(ValueError, match="Datadog API"):
        source("datadog", url="https://attacker.example")
    with pytest.raises(ValueError, match="NerdGraph"):
        source("newrelic", url="https://attacker.example/graphql")


def test_missing_secrets_and_redirects_do_not_fabricate_metrics(monkeypatch):
    monkeypatch.delenv("CORCA_TELEMETRY_DD_KEY", raising=False)
    with pytest.raises(ValueError, match="CORCA_TELEMETRY_DD_KEY"):
        fetch_window(source("datadog"), START, START+60)
    monkeypatch.setenv("CORCA_TELEMETRY_DD_KEY", SECRET)
    monkeypatch.setenv("CORCA_TELEMETRY_DD_APP", SECRET)
    response = Reply({})
    response.status_code = 302
    monkeypatch.setattr(requests, "request", lambda *a, **k: response)
    with pytest.raises(ValueError, match="redirect"):
        fetch_window(source("datadog"), START, START+60)


def test_discovery_includes_missing_configured_services_and_metric_gaps():
    frame = pd.DataFrame({"time": [1, 2, 3], "payments_cpu": [1, None, 3], "payments_mem": [None]*3,
                          "frontend_cpu": [1, 2, 3], "frontend_mem": [1, 2, 3]})
    result = discover_services(frame, source(queries={"cpu": "cpu", "mem": "mem"}, services=["payments", "frontend", "inventory"]))
    services = {service["name"]: service for service in result["services"]}
    assert services["payments"]["status"] == "gaps" and services["payments"]["missing_kinds"] == ["mem"]
    assert services["payments"]["coverage"]["cpu"]["samples"] == 2
    assert services["inventory"]["status"] == "no_data" and services["frontend"]["status"] == "reporting"
    assert result["resource_only"] and not result["topology_configured"] and result["dependencies"] == []


def fake_fetch(config, start, end):
    return pd.DataFrame({"time": [START+i*60 for i in range(30)], "payments_cpu": [10.0]*30, "frontend_cpu": [5.0]*30})


def test_connection_workflow_persists_and_rejects_stale_setup(tmp_path):
    database = tmp_path / "jobs.db"
    with TestClient(create_app(TOKEN, db_path=database, fetch=fake_fetch)) as client:
        assert client.post("/v1/connections", json={"source": source()}).status_code == 401
        assert client.post("/v1/services/discover", headers=HEADERS).status_code == 409
        connected = client.post("/v1/connections", headers=HEADERS, json={"source": source()})
        assert connected.status_code == 200
        revision = connected.json()["revision"]
        assert client.post("/v1/connections", headers=HEADERS, json={"source": source()}).status_code == 409
        discovered = client.post("/v1/services/discover", headers=HEADERS).json()
        assert [service["name"] for service in discovered["services"]] == ["frontend", "payments"]
        topology = {"revision": revision, "services": ["frontend", "payments"], "edges": [["frontend", "payments"]]}
        assert client.post("/v1/topology", headers=HEADERS, json={**topology, "edges": [["frontend", "missing"]]}).status_code == 422
        response = client.post("/v1/topology", headers=HEADERS, json=topology)
        assert response.status_code == 200
        revision = response.json()["revision"]
        assert client.post("/v1/topology", headers=HEADERS, json=topology).status_code == 409
        calibration = {"methods": {"baro": {"tau": 6}}, "stopping_policy": STOPPING_POLICY}
        assert client.post("/v1/calibration", headers=HEADERS, json={"revision": revision, "calibration": {"methods": {"baro": {"tau": 6}}}}).status_code == 422
        assert client.post("/v1/calibration", headers=HEADERS, json={"revision": revision, "calibration": calibration}).status_code == 200
        assert client.get("/v1/config", headers=HEADERS).json()["methods"] == ["baro"]
        assert client.post("/v1/diagnoses", headers=HEADERS, json={"inject_time": time.time(), "normal_seconds": 300}).status_code == 422
        job = client.post("/v1/diagnoses", headers=HEADERS, json={"inject_time": time.time(), "normal_seconds": 600, "anomaly_seconds": 600})
        assert job.status_code == 202
        assert "context" not in job.json() and "url" not in job.text
    with TestClient(create_app(TOKEN, db_path=database, fetch=fake_fetch)) as client:
        assert client.get("/v1/config", headers=HEADERS).json()["methods"] == ["baro"]
        assert client.get("/v1/integrations", headers=HEADERS).json()["prometheus"]["topology"] == "custom"
        response = client.post("/v1/connections", headers=HEADERS, json={"source": source(queries={"mem": "new query"}), "revision": revision})
        assert response.status_code == 200 and client.get("/v1/config", headers=HEADERS).json()["methods"] == []


def test_connection_host_and_secret_reference_boundaries(tmp_path):
    with TestClient(create_app(TOKEN, db_path=tmp_path / "jobs.db")) as client:
        for candidate in (source(prometheus_url="http://169.254.169.254"), source(token_env="AWS_SECRET_ACCESS_KEY")):
            assert client.post("/v1/connections", headers=HEADERS, json={"source": candidate}).status_code == 422
        assert client.post("/v1/connections", headers=HEADERS, json={"source": source("datadog")}).status_code == 200
        metadata = client.get("/v1/integrations", headers=HEADERS)
        assert metadata.json()["provider"] == "datadog" and "CORCA_TELEMETRY" not in metadata.text


def test_queued_job_uses_original_connection_and_calibration(tmp_path, monkeypatch):
    from backend import api
    jobs = Jobs(tmp_path / "jobs.db")
    original = source()
    job = jobs.add("baro", START+600, 600, 600, context={"source": original, "thresholds": {"baro": 6}})[0]
    seen = []

    def fetch(config, start, end):
        seen.append(config)
        return fake_fetch(config, start, end)

    monkeypatch.setattr(api, "diagnose", lambda incident, thresholds, timeout: {"status": "diagnosed", "tau": thresholds["baro"]})
    jobs.finish(job["id"], "queued", ready_at=0)
    claimed = jobs.claim()
    assert claimed["id"] == job["id"]
    run_job(claimed, jobs, source("datadog"), {"baro": 999}, 20, fetch)
    assert seen == [original] and jobs.get(job["id"])["result"]["tau"] == 6


def test_existing_sqlite_database_is_migrated_without_losing_jobs(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, dedupe TEXT UNIQUE, method TEXT NOT NULL, inject_time REAL NOT NULL, start REAL NOT NULL, end REAL NOT NULL, ready_at REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, result TEXT, error TEXT)")
        db.execute("INSERT INTO jobs(id,method,inject_time,start,end,ready_at,status,created) VALUES ('legacy','baro',100,0,200,200,'done',100)")
    jobs = Jobs(path)
    assert jobs.get("legacy")["status"] == "done"
    assert jobs.connection() is None
    assert jobs.add("baro", START, 60, 60)[0]["status"] == "queued"


def test_live_service_ids_escape_underscores_and_percent_without_changing_legacy_names(monkeypatch):
    from corca.topology import metric_column, service_of, metric_kind
    for name in ("payments_service", "payments%5Fservice", "checkout", "payments-db", "payments", "redis-cart", "redis", "~service"):
        column = metric_column(name, "cpu")
        assert service_of(column) == name and metric_kind(column) == "cpu"
    monkeypatch.setattr(requests, "get", lambda *a, **k: Reply({"status": "success", "data": {"result": [
        {"metric": {"service": "payments_service"}, "values": [[START, "1"], [START+60, "2"]]}]}}))
    configured = source()
    frame = fetch_window(configured, START, START+60)
    assert frame.columns.tolist() == ["time", "payments%5Fservice_cpu"]
    assert discover_services(frame, configured)["services"][0]["name"] == "payments_service"


def test_legacy_pending_jobs_are_bound_before_connection_changes(tmp_path):
    database = tmp_path / "jobs.db"
    legacy = Jobs(database).add("baro", time.time(), 600, 600)[0]
    configured = source()
    with TestClient(create_app(TOKEN, {"baro": 6}, source=configured, db_path=database, fetch=fake_fetch)):
        with sqlite3.connect(database) as db:
            context = json.loads(db.execute("SELECT context FROM jobs WHERE id=?", (legacy["id"],)).fetchone()[0])
        assert context["source"] == configured and context["thresholds"] == {"baro": 6}
        assert context["stopping_policy"] == STOPPING_POLICY


def test_duplicate_service_series_and_partial_vendor_pages_are_rejected(monkeypatch):
    for name in ("CORCA_TELEMETRY_DD_KEY", "CORCA_TELEMETRY_DD_APP", "CORCA_TELEMETRY_TOKEN"):
        monkeypatch.setenv(name, SECRET)
    series = {"tag_set": ["service:payments"], "pointlist": [[START*1000, 2]]}
    monkeypatch.setattr(requests, "request", lambda *a, **k: Reply({"status": "ok", "series": [series, series]}))
    with pytest.raises(ValueError, match="Multiple"):
        fetch_window(source("datadog"), START, START+60)
    monkeypatch.setattr(requests, "request", lambda *a, **k: Reply({"nextPageKey": "partial", "result": []}))
    with pytest.raises(ValueError, match="partial page"):
        fetch_window(source("dynatrace"), START, START+60)
    monkeypatch.setattr(requests, "request", lambda *a, **k: Reply({"errors": [{"message": SECRET}]}))
    with pytest.raises(ValueError, match="New Relic rejected") as error:
        fetch_window(source("newrelic"), START, START+60)
    assert SECRET not in str(error.value)


def test_saved_workspace_connection_takes_precedence_over_bootstrap_files(tmp_path):
    path = tmp_path / "jobs.db"
    saved = source("datadog", services=["payments"], edges=[])
    Jobs(path).save_connection({"source": saved, "thresholds": {"baro": 6}})
    with TestClient(create_app(TOKEN, {"baro": 999}, source=source(), db_path=path, fetch=fake_fetch)) as client:
        assert client.get("/v1/integrations", headers=HEADERS).json()["provider"] == "datadog"
        job = client.post("/v1/diagnoses", headers=HEADERS, json={"inject_time": time.time()})
        assert job.status_code == 202
        output = job.json()
        assert output["inject_time"] - output["window"][0] == 600
        assert output["window"][1] - output["inject_time"] == 600
        assert client.post("/v1/diagnoses", headers=HEADERS, json={"inject_time": time.time(), "anomaly_seconds": 120}).status_code == 422


def test_service_selection_respects_metric_column_capacity():
    with pytest.raises(ValueError, match="199 metric"):
        source(services=[f"service-{n}" for n in range(100)], queries={"cpu": "cpu", "mem": "mem"})


def test_saved_calibration_is_invalidated_when_algorithm_policy_changes(tmp_path):
    path = tmp_path / "jobs.db"
    jobs = Jobs(path)
    jobs.save_connection({"source": source(), "thresholds": {"baro": 6}})
    saved = jobs.connection()
    saved["stopping_policy"] = "old-policy"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE settings SET value=? WHERE name='connection'", (json.dumps(saved),))
    with TestClient(create_app(TOKEN, db_path=path)) as client:
        assert client.get("/v1/config", headers=HEADERS).json()["methods"] == []
