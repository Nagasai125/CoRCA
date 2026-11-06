"""Operator checks for a single-team pilot; never grants production approval."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit

import requests

from corca.metrics import set_prf
from backend.admin import copy_database


MANUAL_GATES = ["real_incident_accuracy", "healthy_window_abstention", "alert_delivery_if_used",
                "sustained_load_and_deadlines", "application_recovery_and_rollback", "access_and_operational_owner_review"]


def preflight(url, token=None, verify_metrics=False):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an HTTP(S) deployment URL without credentials, query parameters, or fragments.")
    if token and parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Use HTTPS before sending a deployment token to a remote host.")
    headers = {"X-API-Key": token} if token else {}
    checks, values = [], {}
    endpoints = [("liveness", "/healthz"), ("workspace_storage", "/readyz"),
                 ("configuration", "/v1/config"), ("integration_metadata", "/v1/integrations")]
    if verify_metrics:
        endpoints.append(("live_metrics", "/v1/integrations/check"))
    with requests.Session() as session:
        for name, path in endpoints:
            started = time.monotonic()
            try:
                response = session.request("POST" if name == "live_metrics" else "GET", url.rstrip("/") + path,
                                           headers=headers, timeout=30, allow_redirects=False)
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}; check deployment access or configuration")
                value = response.json()
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
                if name in ("liveness", "workspace_storage") and value.get("status") != "ok":
                    raise ValueError("Health endpoint did not report ok")
                if name == "live_metrics" and (value.get("status") != "verified" or not value.get("metrics") or not value.get("services")):
                    raise ValueError("No verified live metrics")
                values[name] = value
                checks.append({"name": name, "status": "pass", "seconds": round(time.monotonic() - started, 3)})
            except (requests.RequestException, ValueError) as exc:
                # Network errors may contain URLs; don't copy URLs, headers, or response bodies into evidence.
                detail = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, requests.RequestException) else "Request failed; check connectivity and access"
                checks.append({"name": name, "status": "fail", "detail": detail})
    config, metadata = values.get("configuration", {}), values.get("integration_metadata", {})
    source = metadata.get("prometheus", {})
    source = source if isinstance(source, dict) else {}
    for name, passed, detail in [
        ("telemetry_configured", config.get("prometheus") is True and source.get("configured") is True, "Connect a metrics backend in Setup → Connect & verify"),
        ("service_scope", isinstance(source.get("service_count"), int) and source["service_count"] > 0, "Select and save services"),
        ("dependencies_confirmed", source.get("topology") == "custom", "Save actual caller/callee dependencies, or confirm independent services"),
        ("sensitivity_enabled", isinstance(config.get("methods"), list) and bool(config["methods"]) and all(m in ("baro", "circa") for m in config["methods"]), "Set sensitivity on Services"),
    ]:
        checks.append({"name": name, "status": "pass" if passed else "fail", **({} if passed else {"detail": detail})})
    if not verify_metrics:
        checks.append({"name": "live_metrics", "status": "pending", "detail": "Run with --verify-metrics to query the configured backend"})
    return {"kind": "pilot_preflight", "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "needs_setup" if any(c["status"] == "fail" for c in checks) else "awaiting_pilot_acceptance",
            "checks": checks, "provider": metadata.get("provider"), "source_revision": metadata.get("revision"),
            "methods": config.get("methods", []), "threshold_mode": metadata.get("threshold_mode"),
            "manual_gates_pending": MANUAL_GATES, "production_approved": False}


def assess(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get("evidence_kind") not in ("synthetic", "controlled_drill", "real_incident"):
        raise ValueError("Declare evidence_kind as synthetic, controlled_drill, or real_incident")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1000:
        raise ValueError("Manifest must contain 1–1000 cases")
    seen_incidents, seen_jobs, seen_windows, rows = set(), set(), set(), []
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"incident_id", "method", "diagnosis", "root_causes"}:
            raise ValueError("Each case needs incident_id, method, diagnosis, and confirmed root_causes")
        incident_id, truth = case["incident_id"], case["root_causes"]
        if not isinstance(incident_id, str) or not incident_id.strip() or incident_id in seen_incidents:
            raise ValueError("Use one unique nonempty incident_id per independent incident")
        if not isinstance(truth, list) or len(truth) > 100 or any(not isinstance(s, str) or not s.strip() or s != s.strip() or len(s) > 200 for s in truth) or len(set(truth)) != len(truth):
            raise ValueError("root_causes must be unique service names; use [] only for confirmed healthy windows")
        if not isinstance(case["diagnosis"], str) or not case["diagnosis"]:
            raise ValueError("diagnosis must name an exported diagnosis JSON file")
        if case["method"] not in ("baro", "circa"):
            raise ValueError("Each case needs a supported method (baro or circa)")
        seen_incidents.add(incident_id)
        row = {"incident_id": incident_id, "kind": "fault" if truth else "healthy", "truth": truth,
               "method": case["method"], "status": "unavailable", "predicted": [], "F1": 0, "exact": False, "complete_exact": False, "abstained": False}
        try:
            raw = (manifest_path.parent / case["diagnosis"]).read_bytes()
            job = json.loads(raw)
            if isinstance(job, dict) and 'diagnosis' in job:
                job = job['diagnosis']
            if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
                raise ValueError("Expected a complete diagnosis export with a job ID")
            if job["id"] in seen_jobs:
                raise ValueError("Duplicate diagnosis job; repeated exports are not independent incidents")
            seen_jobs.add(job["id"])
            method, incident_time = job.get("method"), job.get("inject_time")
            if method not in ("baro", "circa") or type(incident_time) not in (int, float) or not math.isfinite(incident_time):
                raise ValueError("Diagnosis export needs a valid method and incident time")
            if method != case["method"]:
                raise ValueError("Export method differs from the declared case method")
            window = (method, incident_time)
            if window in seen_windows:
                raise ValueError("Repeated diagnosis of the same incident time and method")
            seen_windows.add(window)
            row.update({"job_id": job["id"], "method": method, "export_sha256": hashlib.sha256(raw).hexdigest()})
            result = job.get("result")
            if job.get("status") != "done" or not isinstance(result, dict):
                row["reason"] = "Diagnosis is not completed; kept in the denominator"
            elif result.get("status") not in ("diagnosed", "partial_unexplained", "partial_timeout", "no_significant_anomaly"):
                row["reason"] = "Unavailable or insufficient evidence; kept in the denominator"
            else:
                predicted = result.get("root_causes")
                if not isinstance(predicted, list) or len(predicted) > 100 or any(not isinstance(s, str) or not s.strip() or s != s.strip() or len(s) > 200 for s in predicted) or len(set(predicted)) != len(predicted):
                    raise ValueError("Invalid predicted root_causes")
                if result['status'] == 'no_significant_anomaly' and predicted:
                    raise ValueError("A no-anomaly result cannot name predicted causes")
                if not predicted and result['status'] != 'no_significant_anomaly':
                    row['reason'] = 'Analysis named no causes before stopping; kept in the denominator'
                    rows.append(row)
                    continue
                exact = set(predicted) == set(truth)
                row.update({"status": result["status"], "predicted": predicted, "F1": set_prf(predicted, truth)[2],
                            "exact": exact, "complete_exact": bool(truth) and exact and result["status"] == "diagnosed",
                            "abstained": not truth and not predicted and result["status"] == "no_significant_anomaly"})
        except OSError:
            row["reason"] = "Export could not be read; kept in the denominator"
        # Malformed exports and duplicate incidents are input errors, not silently excluded evidence.
        rows.append(row)
    methods = {}
    for method in sorted({r.get("method", "unknown") for r in rows}):
        selected = [r for r in rows if r.get("method", "unknown") == method]
        faults, healthy = [r for r in selected if r["kind"] == "fault"], [r for r in selected if r["kind"] == "healthy"]
        evaluated_healthy = [r for r in healthy if r["status"] != "unavailable"]
        methods[method] = {"cases": len(selected), "fault_cases": len(faults), "healthy_cases": len(healthy), "evaluable_healthy_cases": len(evaluated_healthy),
                           "unavailable": sum(r["status"] == "unavailable" for r in selected),
                           "fault_mean_F1": sum(r["F1"] for r in faults) / len(faults) if faults else None,
                           "fault_exact_match_rate": sum(r["exact"] for r in faults) / len(faults) if faults else None,
                           "complete_fault_exact_match_rate": sum(r["complete_exact"] for r in faults) / len(faults) if faults else None,
                           "healthy_abstention_rate": sum(r["abstained"] for r in healthy) / len(healthy) if healthy else None,
                           "healthy_false_positive_rate": sum(bool(r["predicted"]) for r in evaluated_healthy) / len(evaluated_healthy) if evaluated_healthy else None}
    return {"kind": "pilot_case_assessment", "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "evidence_kind": manifest["evidence_kind"],
            "cases": rows, "by_method": methods, "production_approved": False,
            "limitations": ["Operator-confirmed labels; this report cannot establish independence or an untouched test split",
                            "Unavailable cases count as zero for fault scores and healthy abstention; false-positive rate uses only evaluable healthy cases",
                            "Partial predictions are distinct from complete exact results",
                            "Exports do not prove unchanged source, dependencies, thresholds, or hardware; freeze and record the deployment separately",
                            "Agree sample sizes and acceptance criteria before evaluating; this report has no automatic quality pass"]}


def recovery(db_path, out_directory):
    """Exercise SQLite snapshot/restore in isolation without starting another worker."""
    out = Path(out_directory)
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="corca-recovery-", dir=out) as directory:
        backup, restored = Path(directory) / "backup.db", Path(directory) / "restored.db"
        copy_database(db_path, backup)
        copy_database(backup, restored)
        def state(path):
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                digest = hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()
                counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("jobs", "settings")}
                return digest, counts
        original, recovered = state(backup), state(restored)
        if original != recovered:
            raise ValueError("Restored snapshot differs from backup")
    return {"kind": "pilot_storage_recovery", "status": "pass", "seconds": round(time.monotonic() - started, 3),
            "logical_snapshot_sha256": original[0], "rows": original[1], "production_approved": False,
            "limitations": ["Isolated SQLite snapshot/restore only; no application restart, rollback, off-host backup, or secret recovery was verified",
                            "Temporary database copies were deleted; this drill is not a retained backup"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("preflight", help="Read deployment state; optionally verify recent telemetry")
    check.add_argument("--url", default="http://localhost:8000")
    check.add_argument("--token-env", default="CORCA_API_TOKEN")
    check.add_argument("--verify-metrics", action="store_true")
    scoring = commands.add_parser("assess", help="Score exported diagnoses against confirmed incident labels")
    scoring.add_argument("--manifest", required=True)
    restore = commands.add_parser("recovery", help="Exercise storage backup/restore without modifying the source")
    restore.add_argument("--db", default=os.getenv("CORCA_DB", "backend/.state/corca.db"))
    restore.add_argument("--work-dir", required=True)
    for command in (check, scoring, restore):
        command.add_argument("--out", required=True, help="New report path; existing files are never overwritten")
    args = parser.parse_args()
    try:
        destination = Path(args.out)
        if destination.exists():
            raise ValueError("Report already exists; use a new --out path")
        if args.command == "preflight":
            report = preflight(args.url, os.getenv(args.token_env), args.verify_metrics)
        elif args.command == "assess":
            report = assess(args.manifest)
        else:
            report = recovery(args.db, args.work_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Pilot check failed: {exc}\n")
    print(f"Saved {report['kind']} to {destination}. Production approval remains pending.")
    if report.get("status") == "needs_setup":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
