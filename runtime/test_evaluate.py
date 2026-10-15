import json

import pandas as pd
import pytest

from corca.circa import FEATURE_POLICY, InsufficientEvidence
from corca.corca import STOPPING_POLICY
from runtime.evaluate import evaluate, experiment_config, score, validate_fixed_calibration, write_reports
from scripts.summarize import calibrate


def record(method="baro", truth=None, status="ok", residual=5):
    return {"case": "a/1", "case_id": "1", "method": method, "truth": truth or ["a"],
            "drop": 0, "seed": 0, "keep_resource": True, "graph_mode": "observed",
            "base_rank": ["a", "b"], "rounds": [{"service": "a", "residual": residual, "confidence": .8}],
            "status": status, "seconds": 1, "baseline_seconds": .5}


def test_report_discloses_uncalibrated_method_and_failures(tmp_path):
    bad = record(method="circa", status="error")
    bad.update(base_rank=[], rounds=[], error="original graph error", error_stage="baseline")
    records = [record(), bad, record(truth=["a", "b"]), dict(bad, truth=["a", "b"])]
    report = write_reports(tmp_path, records, ["baro", "circa"], expected=4)
    assert report["status_counts"] == {"baro": {"ok": 2}, "circa": {"error": 2}}
    assert report["missing"] == 0
    unavailable = report["calibration"]["unavailable_methods"]["circa"]
    assert unavailable["n_cases"] == 1 and unavailable["n_correct_first"] == 0
    aggregate = pd.read_csv(tmp_path / "aggregate.csv")
    row = aggregate[aggregate.method == "circa"].iloc[0]
    assert not row.calibrated and row.failure_rate == 1 and row.corca_F1 == 0
    assert len(report["failures"]) == 2


def test_failed_and_timeout_controls_cannot_calibrate():
    with pytest.raises(ValueError, match="no correctly ranked"):
        calibrate([record(status="error"), record(status="timeout")], .9)


def test_controls_only_report_is_valid(tmp_path):
    write_reports(tmp_path, [record()], ["baro"], expected=1)
    assert pd.read_csv(tmp_path / "aggregate.csv").empty


def test_scoring_separates_execution_from_remaining_evidence(tmp_path):
    rec = dict(record(truth=["a", "b"], residual=11), stopping_policy=STOPPING_POLICY,
               feature_policy="rcaeval-baro-v1")
    calibration = {"methods": {"baro": {"tau": 10}}, "stopping_policy": STOPPING_POLICY,
                   "feature_policies": {"baro": "rcaeval-baro-v1"}}
    report = write_reports(tmp_path, [rec], ["baro"], fixed_calibration=calibration)
    assert report["status_counts"] == {"baro": {"ok": 1}}
    assert report["diagnosis_status_counts"] == {"baro": {"partial_unexplained": 1}}
    frame = pd.read_csv(tmp_path / "summary.csv")
    assert frame.iloc[0].remaining_anomaly_score == 11
    assert score([rec], {}).iloc[0].diagnosis_status == "uncalibrated"
    rec["rounds"][0]["residual"] = 10
    rec["rounds"].append({"service": "b", "residual": 100, "confidence": .5})
    # A tau=None diagnostic run can continue after the calibrated live loop would stop.
    row = score([rec], {"baro": 10}).iloc[0]
    assert row.diagnosis_status == "diagnosed" and row.remaining_anomaly_score == 10
    assert json.loads(row.predicted) == ["a"]


def test_evaluator_records_input_rejection_and_failed_baseline_time(monkeypatch, tmp_path):
    from runtime import evaluate as module
    folder = tmp_path / "case" / "1"
    folder.mkdir(parents=True)
    (folder / "ground_truth.json").write_text(json.dumps({"root_causes": ["a"]}))
    monkeypatch.setattr(module, "incident_payload", lambda *a, **k: {
        "columns": ["time", "a_cpu"], "rows": [[i, i] for i in range(30)], "inject_time": 15})
    def fail(*args):
        raise InsufficientEvidence("singular input")
    monkeypatch.setattr(module, "call_with_timeout", fail)
    result = evaluate(folder, "circa", 0, 0, 20)
    assert result["status"] == "insufficient_evidence"
    assert result["error_stage"] == "baseline" and result["baseline_seconds"] > 0
    assert result["error"] == "singular input" and result["base_rank"] == []


def test_fixed_calibration_scores_single_fault_holdouts_without_refitting(tmp_path):
    calibration = {"methods": {"circa": {"tau": 10}}, "feature_policies": {"circa": FEATURE_POLICY},
                   "stopping_policy": STOPPING_POLICY,
                   "training_metrics_sha256": {"training/metrics.csv": "train-hash"}}
    validate_fixed_calibration(calibration, ["circa"], {"test/metrics.csv": "test-hash"})
    rec = dict(record(method="circa", residual=5), feature_policy=FEATURE_POLICY, stopping_policy=STOPPING_POLICY)
    report = write_reports(tmp_path, [rec], ["circa"], expected=1, fixed_calibration=calibration)
    assert report["calibration"] == calibration
    assert report["calibration_mode"] == "fixed_separate_controls"
    assert pd.read_csv(tmp_path / "summary.csv").iloc[0].corca_exact == 1


def test_fixed_calibration_rejects_overlap_missing_method_and_old_policy():
    calibration = {"methods": {"circa": {"tau": 10}}, "feature_policies": {"circa": FEATURE_POLICY},
                   "stopping_policy": STOPPING_POLICY,
                   "training_metrics_sha256": {"training/metrics.csv": "same-hash"}}
    with pytest.raises(ValueError, match="calibration incidents"):
        validate_fixed_calibration(calibration, ["circa"], {"renamed/metrics.csv": "same-hash"})
    with pytest.raises(ValueError, match="missing a requested method"):
        validate_fixed_calibration(calibration, ["baro"], {"test": "new"})
    calibration["feature_policies"]["circa"] = "old-policy"
    with pytest.raises(ValueError, match="recalibrate"):
        validate_fixed_calibration(calibration, ["circa"], {"test": "new"})


def test_reports_cannot_pool_feature_policies(tmp_path):
    records = [dict(record(method="circa"), feature_policy=FEATURE_POLICY), record(method="circa")]
    with pytest.raises(ValueError, match="cannot pool"):
        write_reports(tmp_path, records, ["circa"])


def test_manifest_covers_workers_injection_boundary_and_reporting_sources(tmp_path):
    from argparse import Namespace
    folder = tmp_path / "data" / "a_cpu" / "1"
    folder.mkdir(parents=True)
    (folder / "metrics.csv").write_text("time,a_cpu\n0,1\n1,10\n")
    (folder / "inject_time.txt").write_text("1")
    (folder / "ground_truth.json").write_text('{"root_causes": ["a"]}')
    args = Namespace(data=str(tmp_path / "data"), methods=["circa"], rates=[0], seeds=[0],
                     timeout=20, workers=1, calibration=None)
    first = experiment_config(args, [folder])
    assert first["feature_policies"]["circa"] == FEATURE_POLICY
    assert "scripts/summarize.py" in first["source"]
    args.workers = 2
    assert experiment_config(args, [folder]) != first
    args.workers = 1
    (folder / "inject_time.txt").write_text("2")
    assert experiment_config(args, [folder]) != first
