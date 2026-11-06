import json
import hashlib
import sqlite3

from fastapi.testclient import TestClient
import pandas as pd
import pytest
import requests

from backend.api import Jobs, create_app
from backend.pilot import assess, preflight, recovery
from backend.telemetry import validate_source


def bridge(monkeypatch, client):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, url, **kwargs):
            assert kwargs.pop("allow_redirects") is False
            kwargs.pop("timeout")
            return client.request(method, url, follow_redirects=False, **kwargs)
    monkeypatch.setattr(requests, "Session", Session)


def test_preflight_never_promotes_unconfigured_workspace(monkeypatch, tmp_path):
    with TestClient(create_app(db_path=tmp_path / "workspace.db")) as client:
        bridge(monkeypatch, client)
        report = preflight("http://testserver")
        checks = {c["name"]: c["status"] for c in report["checks"]}
        assert checks["workspace_storage"] == "pass"
        assert checks["telemetry_configured"] == "fail"
        assert checks["live_metrics"] == "pending"
        assert report["status"] == "needs_setup" and not report["production_approved"]
        assert report["manual_gates_pending"]


def test_configured_preflight_queries_metrics_only_when_requested(monkeypatch, tmp_path):
    source = validate_source({"prometheus_url": "http://localhost:9090", "queries": {"cpu": "cpu"},
                              "services": ["cart"], "edges": [], "step": 5})
    calls = []
    def fetch(*args):
        calls.append(args)
        return pd.DataFrame({"time": [1, 2], "cart_cpu": [1, 2]})
    with TestClient(create_app("long-enough-token", {"baro": 6}, source=source, db_path=tmp_path / "workspace.db", fetch=fetch)) as client:
        bridge(monkeypatch, client)
        report = preflight("http://localhost", "long-enough-token")
        assert report["status"] == "awaiting_pilot_acceptance" and not calls
        report = preflight("http://localhost", "long-enough-token", verify_metrics=True)
        assert len(calls) == 1 and all(c["status"] == "pass" for c in report["checks"])
        assert not report["production_approved"]
        encoded = json.dumps(report)
        assert "long-enough-token" not in encoded and "9090" not in encoded and '"queries"' not in encoded
        denied = preflight("http://localhost", "incorrect-key")
        assert denied["status"] == "needs_setup"


@pytest.mark.parametrize("url,token", [("http://remote.example", "secret"), ("http://user:secret@localhost", None), ("file:///tmp/data", None)])
def test_preflight_does_not_send_credentials_to_unsafe_urls(url, token):
    with pytest.raises(ValueError):
        preflight(url, token)


def manifest(tmp_path, jobs, truths):
    cases = []
    for i, (job, truth) in enumerate(zip(jobs, truths)):
        path = tmp_path / f"case-{i}.json"
        if job is not None:
            path.write_text(json.dumps(job))
        cases.append({"incident_id": f"incident-{i}", "method": "baro", "diagnosis": path.name, "root_causes": truth})
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"evidence_kind": "synthetic", "cases": cases}))
    return path


def job(i, roots, result_status="diagnosed", status="done"):
    return {"id": str(i), "method": "baro", "inject_time": 1000 + i,
            "status": status, "result": {"root_causes": roots, "status": result_status}}


def test_assessment_keeps_failures_and_partial_results_in_denominators(tmp_path):
    path = manifest(tmp_path, [job(1, ["cart"]), job(2, ["cart"], "partial_unexplained"),
                               job(3, [], status="failed"), job(4, [], "no_significant_anomaly"), None],
                    [["cart"], ["cart"], ["cart"], [], []])
    report = assess(path)
    scores = report["by_method"]["baro"]
    assert scores["cases"] == 5 and scores["unavailable"] == 2
    assert scores["fault_exact_match_rate"] == 2 / 3
    assert scores["complete_fault_exact_match_rate"] == 1 / 3
    assert scores["fault_mean_F1"] == 2 / 3
    assert scores["healthy_abstention_rate"] == 1 / 2
    assert report["evidence_kind"] == "synthetic" and not report["production_approved"]
    assert all("export_sha256" in r for r in report["cases"] if r["status"] != "unavailable")


def test_duplicate_or_inconsistent_exports_are_rejected(tmp_path):
    path = manifest(tmp_path, [job(1, ["cart"]), job(1, ["cart"])], [["cart"], ["cart"]])
    with pytest.raises(ValueError, match="Duplicate diagnosis"):
        assess(path)
    path = manifest(tmp_path, [job(1, ["cart"])], [["cart"]])
    data = json.loads(path.read_text()); data["cases"][0]["method"] = "circa"; path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="method differs"):
        assess(path)


def test_evidence_bundles_keep_file_provenance_and_legacy_scoring(tmp_path):
    original = job(1, ['cart'])
    path = manifest(tmp_path, [original], [['cart']])
    legacy = assess(path)
    exported = tmp_path / 'case-0.json'
    exported.write_text(json.dumps({'diagnosis':original,'evidence':{'series':[]},'review':None}))
    bundled = assess(path)
    assert bundled['by_method']==legacy['by_method']
    assert bundled['cases'][0]['export_sha256']==hashlib.sha256(exported.read_bytes()).hexdigest()
    assert bundled['cases'][0]['export_sha256']!=legacy['cases'][0]['export_sha256']


def test_stopped_empty_predictions_are_unavailable_not_healthy_evidence(tmp_path):
    path = manifest(tmp_path, [job(1, [], 'partial_timeout'),job(2, [], 'partial_unexplained'),job(3, [], 'diagnosed'),job(4, [], 'no_significant_anomaly')], [[],[],[],[]])
    scores = assess(path)['by_method']['baro']
    assert scores['cases']==4 and scores['unavailable']==3
    assert scores['evaluable_healthy_cases']==1 and scores['healthy_false_positive_rate']==0
    assert scores['healthy_abstention_rate']==.25


@pytest.mark.parametrize('roots,status', [(['cart'],'no_significant_anomaly'),(['cart','cart'],'diagnosed'),([' cart'],'diagnosed')])
def test_inconsistent_or_ambiguous_predictions_are_input_errors(tmp_path,roots,status):
    path = manifest(tmp_path,[job(1,roots,status)],[['cart']])
    with pytest.raises(ValueError):
        assess(path)


def test_recovery_does_not_modify_live_workspace_or_keep_temporary_copies(tmp_path):
    database = tmp_path / "workspace.db"
    jobs = Jobs(database)
    jobs.add("baro", 1000, 300, 120)
    jobs.save_connection({"source": {"credential_reference": "CORCA_TELEMETRY_TOKEN"}, "thresholds": {"baro": 6}})
    with sqlite3.connect(database) as db:
        before = list(db.iterdump())
    report = recovery(database, tmp_path / "drill")
    assert report["status"] == "pass" and report["rows"] == {"jobs": 1, "settings": 1}
    with sqlite3.connect(database) as db:
        assert list(db.iterdump()) == before
    assert not list((tmp_path / "drill").iterdir())
    assert not report["production_approved"]
    assert "CORCA_TELEMETRY_TOKEN" not in json.dumps(report)


def test_cli_reports_are_private_and_never_overwritten(tmp_path, monkeypatch):
    from backend import pilot
    report = tmp_path / "report.json"
    monkeypatch.setattr(pilot, "preflight", lambda *args: {"kind": "pilot_preflight", "status": "needs_setup", "production_approved": False})
    monkeypatch.setattr("sys.argv", ["pilot", "preflight", "--out", str(report)])
    with pytest.raises(SystemExit) as exc:
        pilot.main()
    assert exc.value.code == 2 and report.stat().st_mode & 0o777 == 0o600
    before = report.read_bytes()
    with pytest.raises(SystemExit) as exc:
        pilot.main()
    assert exc.value.code == 1 and report.read_bytes() == before
