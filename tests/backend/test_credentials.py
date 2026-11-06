import json
import sqlite3
import time

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
import pytest
import requests

from backend.admin import copy_database
from backend.api import create_app, Jobs
from backend.credentials import credential_value
from tests.backend.test_telemetry_sources import source, Reply
from backend.telemetry import fetch_window

SECRET = "customer-api-token-do-not-return"
APP_KEY = "customer-application-key-do-not-return"


@pytest.mark.parametrize("provider", ["prometheus", "grafana", "datadog", "dynatrace", "newrelic"])
def test_direct_credentials_verified_saved_privately_and_resolved_after_restart(tmp_path, provider):
    database = tmp_path / "workspace.db"
    expected = {"api_key": SECRET, "app_key": APP_KEY} if provider == "datadog" else {"token": SECRET}
    candidate = source(provider)
    for field in ("token_env", "api_key_env", "app_key_env"):
        candidate.pop(field, None)
    calls = []
    def fetch(configured, start, end):
        calls.append(configured)
        field = "api_key_env" if provider == "datadog" else "token_env"
        assert credential_value(configured, configured[field]) == SECRET
        if provider == "datadog":
            assert credential_value(configured, configured["app_key_env"]) == APP_KEY
        return pd.DataFrame({"time": [start, end], "cart_cpu": [1, 2]})
    with TestClient(create_app(db_path=database, fetch=fetch)) as client:
        connected = client.post("/v1/connections", json={"source": candidate, "credentials": expected})
        assert connected.status_code == 200, connected.text
        assert connected.json()["verification"]["status"] == "verified"
        assert connected.json()["discovery"]["services"][0]["name"] == "cart"
        for path in ("/v1/integrations", "/v1/config", "/v1/services", "/v1/diagnoses", "/openapi.json"):
            response = client.get(path)
            assert SECRET not in response.text and APP_KEY not in response.text
        assert SECRET not in connected.text and APP_KEY not in connected.text
    with sqlite3.connect(database) as db:
        dump = "\n".join(db.iterdump())
    assert SECRET not in dump and APP_KEY not in dump
    files = list((tmp_path / "credentials").glob("*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "credentials").stat().st_mode & 0o777 == 0o700
    copy_database(database, tmp_path / "backup.db")
    assert SECRET.encode() not in (tmp_path / "backup.db").read_bytes()
    with TestClient(create_app(db_path=database, fetch=fetch)) as client:
        assert client.post("/v1/integrations/check").status_code == 200
    assert len(calls) == 2


def test_failed_verification_preserves_settings_and_does_not_store_secrets(tmp_path, caplog):
    database = tmp_path / "workspace.db"
    def fetch(*_):
        response = requests.Response(); response.status_code = 401
        raise requests.HTTPError(SECRET, response=response)
    with TestClient(create_app(db_path=database, fetch=fetch)) as client:
        saved = client.post("/v1/connections", json={"source": source()}).json()
        before = client.get("/v1/integrations").json()
        denied = client.post("/v1/connections", json={"source": source(), "credentials": {"token": SECRET}, "revision": saved["revision"]})
        assert denied.status_code == 422 and "rejected" in denied.text
        assert client.get("/v1/integrations").json() == before
        assert SECRET not in denied.text and SECRET not in caplog.text
        assert not (tmp_path / "credentials").exists()
        malformed = client.post("/v1/connections", json={"source": source(), "credentials": {SECRET: {"token": SECRET}}})
        assert malformed.status_code == 422 and SECRET not in malformed.text


def test_invalid_hosts_and_storage_failures_do_not_leave_credentials(tmp_path, monkeypatch):
    calls = []
    def fetch(*args):
        calls.append(args)
        return pd.DataFrame({"time": [1, 2], "cart_cpu": [1, 2]})
    with TestClient(create_app(db_path=tmp_path / "workspace.db", fetch=fetch)) as client:
        blocked = client.post("/v1/connections", json={"source": source(prometheus_url="https://unapproved.example"), "credentials": {"token": SECRET}})
        assert blocked.status_code == 422 and not calls
        def fail(*args):
            raise sqlite3.OperationalError("unavailable")
        monkeypatch.setattr(Jobs, "save_connection", fail)
        failed = client.post("/v1/connections", json={"source": source(), "credentials": {"token": SECRET}})
        assert failed.status_code == 503
        assert not list((tmp_path / "credentials").glob("*.json"))


def test_saved_credential_references_reach_actual_adapter_headers(monkeypatch):
    start = int(time.time()) // 60 * 60
    bodies = {
        "datadog": {"status": "ok", "series": [{"tag_set": ["service:cart"], "pointlist": [[start*1000, 1], [(start+60)*1000, 2]]}]},
        "dynatrace": {"result": [{"data": [{"dimensionMap": {"service": "cart"}, "timestamps": [start*1000, (start+60)*1000], "values": [1, 2]}]}]},
        "newrelic": {"data": {"actor": {"account": {"nrql": {"results": [{"facet": ["cart"], "beginTimeSeconds": start, "value": 1}, {"facet": ["cart"], "beginTimeSeconds": start+60, "value": 2}]}}}}},
    }
    for provider in ("datadog", "dynatrace", "newrelic", "grafana", "prometheus"):
        configured = source(provider)
        names = [configured[k] for k in ("token_env", "api_key_env", "app_key_env") if k in configured]
        if not names:
            configured["token_env"] = "CORCA_TELEMETRY_TEST_TOKEN"; names = [configured["token_env"]]
        configured["_credential_values"] = {name: SECRET for name in names}
        calls = []
        def request(*args, **kwargs):
            calls.append(kwargs)
            return Reply(bodies[provider])
        def get(*args, **kwargs):
            calls.append(kwargs)
            return Reply({"status": "success", "data": {"result": [{"metric": {"service": "cart"}, "values": [[start, "1"], [start+60, "2"]]}]}})
        monkeypatch.setattr(requests, "request", request); monkeypatch.setattr(requests, "get", get)
        frame = fetch_window(configured, start, start+60)
        assert frame.shape[1] == 2
        assert SECRET in json.dumps(calls)
