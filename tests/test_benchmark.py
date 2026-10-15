import argparse
import json
import multiprocessing as mp
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from corca import corca
from corca.base import anomaly_scores, rank_metrics
from corca.execution import call_with_timeout
from corca.synthetic import baro_like_rank, generate
from scripts.experiment import prepare_experiment, read_runs
from scripts.run_eval import evaluate_case
from scripts.summarize import diagnosis_evidence, predicted_set, summarize


def catches_errors():
    try:
        time.sleep(5)
    except Exception:
        return "fallback"


def test_timeout_in_thread_cannot_be_swallowed_and_reaps_worker():
    before = {p.pid for p in mp.active_children()}
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call_with_timeout, catches_errors, 0.5)
        with pytest.raises(TimeoutError):
            future.result(timeout=3)
    assert {p.pid for p in mp.active_children()} == before


def test_rcaeval_fallback_is_an_error(monkeypatch):
    from corca import base
    monkeypatch.setattr(base, "_rcaeval", lambda name: lambda *a, **k: {"adj": [], "ranks": ["a_cpu"]})
    with pytest.raises(RuntimeError, match="fallback"):
        rank_metrics("baro", pd.DataFrame({"time": [0, 1], "a_cpu": [1, 2]}), 1)


def test_flat_shift_is_anomalous_but_constant_and_missing_metrics_are_not():
    df = pd.DataFrame({"time": [0, 1, 2, 3], "a_cpu": [1, 1, 10, 10],
                       "b_cpu": [1, 1, 1, 1], "c_cpu": [float("nan")] * 4})
    scores = anomaly_scores(df, 2)
    assert scores["a_cpu"] > 100
    assert "b_cpu" not in scores and "c_cpu" not in scores


def test_cached_first_round_matches_full_run():
    df, t = generate(["currencyservice", "adservice"], seed=2)
    expected = corca(df, t, rank_fn=baro_like_rank, tau=6)
    actual = corca(df, t, rank_fn=baro_like_rank, initial_rank=baro_like_rank(df, t), tau=6)
    assert actual == expected


@pytest.mark.parametrize("offset, expected", [(-.001, 2), (0, 1), (.001, 1)])
def test_live_and_offline_stopping_agree_at_threshold(offset, expected):
    df, t = generate(["currencyservice", "adservice"], seed=2)
    full = corca(df, t, rank_fn=baro_like_rank, max_rounds=2, tau=None)
    tau = full.rounds[0].residual + offset
    live = corca(df, t, rank_fn=baro_like_rank, max_rounds=2, tau=tau)
    offline = predicted_set({"rounds": [vars(r) for r in full.rounds]}, tau)
    assert len(live.root_causes) == expected
    assert offline == live.root_causes


@pytest.mark.parametrize("status,residual,tau,expected", [
    ("ok", 11, 10, "partial_unexplained"), ("ok", 10, 10, "diagnosed"),
    ("timeout", 11, 10, "partial_timeout"), ("timeout", 10, 10, "diagnosed"),
    ("error", 10, 10, "error"),
    ("ok", 11, None, "uncalibrated")])
def test_offline_diagnosis_status(status, residual, tau, expected):
    record = {"status": status, "rounds": [{"service": "a", "residual": residual}]}
    evidence = diagnosis_evidence(record, tau)
    assert evidence["diagnosis_status"] == expected
    assert evidence["remaining_anomaly_score"] == (residual if tau is not None else None)


def test_zero_error_baseline_does_not_create_billion_scale_residuals():
    df = pd.DataFrame({"time": [0, 1, 2, 3], "a_error": [0, 0, .2, .3],
                       "a_log-error": [0, 0, 1, 2]})
    scores = anomaly_scores(df, 2)
    assert 0 < scores["a_error"] <= 100
    assert 0 < scores["a_log-error"] <= 2


def test_manifest_rejects_changed_parameters_and_data(tmp_path):
    from corca import Case
    folder = tmp_path / "data" / "a_cpu" / "1"
    folder.mkdir(parents=True)
    (folder / "data.csv").write_text("time,a_cpu\n0,1\n1,10\n")
    (folder / "inject_time.txt").write_text("1")
    case = Case(str(folder), ["a"], ["cpu"], "1")
    args = argparse.Namespace(data=str(tmp_path / "data"), out=str(tmp_path / "out"),
                              methods=["baro"], dataset="re1-ob", max_rounds=3,
                              no_keep_resource=False, round_timeout=60, seed=0, drop_telemetry=0)
    identity = prepare_experiment(args, [case])
    assert prepare_experiment(args, [case]) == identity
    args.no_keep_resource = True
    with pytest.raises(ValueError, match="changed"):
        prepare_experiment(args, [case])
    args.no_keep_resource = False
    (folder / "data.csv").write_text("time,a_cpu\n0,1\n1,20\n")
    with pytest.raises(ValueError, match="changed"):
        prepare_experiment(args, [case])


def test_resume_uses_latest_attempt_and_rejects_damaged_record(tmp_path):
    path = tmp_path / "runs.jsonl"
    records = [{"case": "a/1", "method": "baro", "experiment_id": "x", "status": s}
               for s in ["error", "ok"]]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    assert read_runs(path, "x") == [records[-1]]
    with pytest.raises(ValueError, match="another experiment"):
        read_runs(path, "y")
    with path.open("a") as stream:
        stream.write('{"case":')
    with pytest.raises(ValueError, match="invalid result"):
        read_runs(path, "x")


def test_baseline_timeout_is_recorded(tmp_path):
    from corca import Case
    folder = tmp_path / "a_cpu" / "1"
    folder.mkdir(parents=True)
    (folder / "data.csv").write_text("time,a_cpu\n0,1\n1,10\n")
    (folder / "inject_time.txt").write_text("1")
    args = argparse.Namespace(data=str(tmp_path), method="baro", dataset="re1-ob",
                              max_rounds=3, no_keep_resource=False, round_timeout=0.001,
                              seed=0, drop_telemetry=0)
    record = evaluate_case(Case(str(folder), ["a"], ["cpu"], "1"), args, "x")
    assert record["status"] == "timeout" and record["error_stage"] == "baseline"
    assert record["timed_out"] and not record["rounds"]
    assert record["baseline_seconds"] > 0


def test_held_out_calibration_and_failure_denominator():
    records = [{"case": "a/" + cid, "case_id": cid, "method": "baro", "truth": ["a"],
                "base_rank": ["a"], "rounds": [{"service": "a", "residual": residual}],
                "status": "ok"} for cid, residual in [("1", 2), ("2", 20)]]
    records.append({"case": "b/1", "case_id": "1", "method": "baro", "truth": ["b"],
                    "base_rank": [], "rounds": [], "status": "error"})
    frame, calibration = summarize(records)
    assert frame["tau"].tolist() == [20, 2, 20]
    assert frame["top1"].mean() == pytest.approx(2 / 3)
    assert frame["corca_F1"].mean() == pytest.approx(2 / 3)
    assert calibration["baro"]["n_training"] == 2
    with pytest.raises(ValueError, match="held-out"):
        summarize(records[:1])
    multi = dict(records[0], truth=["a", "b"])
    with pytest.raises(ValueError, match="multi-fault"):
        summarize([multi])
