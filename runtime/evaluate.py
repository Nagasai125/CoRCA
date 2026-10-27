"""Evaluate collected incidents, fixed calibration, telemetry loss, and ablations."""
import argparse
import hashlib
import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from importlib import metadata
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

from corca import corca
from corca.base import rank_metrics, services_from_metrics
from corca.circa import FEATURE_POLICY, InsufficientEvidence
from corca.corca import STOPPING_POLICY
from corca.execution import call_with_timeout
from corca.metrics import (TOP_K_BASELINES, TOP_K_COLUMNS, exact_match, set_prf,
                           top_k_set_scores)
from runtime.api import Incident, calibration_thresholds, prepare_window
from runtime.client import incident_payload
from scripts.summarize import calibrate, calibration_residuals, diagnosis_evidence, predicted_set, result_policies, validate_calibration_split


def evaluate(folder, method, drop, seed, timeout, keep_resource=True, graph_mode="observed"):
    started = time.perf_counter()
    truth = json.loads((folder / "ground_truth.json").read_text())["root_causes"]
    record = {"case": str(folder), "case_id": folder.name, "method": method, "truth": truth,
              "drop": drop, "seed": seed, "keep_resource": keep_resource, "graph_mode": graph_mode,
              "base_rank": [], "rounds": [], "status": "error", "baseline_seconds": 0.0}
    record["feature_policy"] = FEATURE_POLICY if method == "circa" else "rcaeval-baro-v1"
    record["stopping_policy"] = STOPPING_POLICY
    stage = "prepare"
    try:
        incident = Incident(**incident_payload(folder, method=method, drop=drop, seed=seed))
        df, dropped, missing = prepare_window(incident)
        record["dropped_metrics"] = dropped
        record["graph_excluded_metrics"] = [c for c in df if method == "circa" and c.endswith("_workload")]
        graph = nx.DiGraph(incident.edges) if graph_mode == "observed" else None
        rank = partial(rank_metrics, method, dataset="re2-ob", seed=0)
        stage = "baseline"
        baseline_start = time.perf_counter()
        try:
            initial = call_with_timeout(rank, timeout, (df, incident.inject_time))
        finally:
            record["baseline_seconds"] = time.perf_counter() - baseline_start
        record["base_rank"] = services_from_metrics(initial)
        stage = "corca"
        result = corca(df, incident.inject_time, rank_fn=rank, initial_rank=initial, graph=graph,
                       max_rounds=3, tau=None, keep_resource=keep_resource, round_timeout=timeout)
        record["rounds"] = [vars(r) for r in result.rounds]
        record["status"] = "timeout" if result.timed_out else "ok"
        if result.timed_out:
            record["error_stage"] = stage
    except Exception as exc:
        record["status"] = ("insufficient_evidence" if isinstance(exc, InsufficientEvidence) else
                            "timeout" if isinstance(exc, TimeoutError) else "error")
        record["error"] = str(exc)
        record["error_stage"] = stage
    record["seconds"] = time.perf_counter() - started
    return record


def score(records, taus):
    rows = []
    for record in records:
        tau = taus.get(record["method"])
        pred = predicted_set(record, tau) if tau is not None else []
        truth = record["truth"]
        p, r, f = set_prf(pred, truth)
        _, _, base_f = set_prf(record["base_rank"][:len(truth)], truth)
        rows.append({"case": record["case"], "method": record["method"], "n_true": len(truth),
                     "drop": record["drop"], "seed": record["seed"], "keep_resource": record["keep_resource"],
                     "graph_mode": record["graph_mode"], "status": record["status"],
                     **diagnosis_evidence(record, tau),
                     "calibrated": tau is not None, "tau": tau, "predicted": json.dumps(pred),
                     "execution_success_rate": float(record["status"] == "ok"),
                     "failure_rate": float(record["status"] == "error"),
                     "timeout_rate": float(record["status"] == "timeout"),
                     "insufficient_evidence_rate": float(record["status"] == "insufficient_evidence"),
                     "base_F1": base_f, **top_k_set_scores(record["base_rank"], truth),
                     "corca_P": p, "corca_R": r, "corca_F1": f,
                     "corca_exact": exact_match(pred, truth), "n_pred": len(pred),
                     "seconds": record["seconds"], "baseline_seconds": record["baseline_seconds"]})
    return pd.DataFrame(rows)


def write_reports(output, records, methods, expected=None, fixed_calibration=None, training_metrics=None):
    versions = result_policies(records)
    stopping_policy = versions["stopping_policy"]
    policies = versions["feature_policies"]
    training = [r for r in records if len(r["truth"]) == 1 and r["drop"] == 0
                and r["keep_resource"] and r["graph_mode"] == "observed"]
    thresholds, calibrated, unavailable = {}, {}, {}
    for method in (methods if fixed_calibration is None else []):
        subset = [r for r in training if r["method"] == method]
        details = {"n_cases": len(subset), "n_correct_first": len(calibration_residuals(subset)),
                   "status_counts": dict(Counter(r["status"] for r in subset))}
        try:
            thresholds[method] = calibrate(subset, .9)
            calibrated[method] = {"tau": thresholds[method], **details}
        except ValueError as exc:
            unavailable[method] = {"reason": str(exc), **details}
    calibration = {"methods": calibrated, "unavailable_methods": unavailable,
                   "source": "separate_local_single_faults", "q": .9,
                   "feature_policies": policies, "stopping_policy": stopping_policy,
                   "training_metrics_sha256": training_metrics or {}}
    if fixed_calibration is not None:
        thresholds = calibration_thresholds(fixed_calibration)
        if any(fixed_calibration.get("feature_policies", {}).get(m) != policies[m] for m in methods):
            raise ValueError("calibration and evaluation feature policies differ")
        if fixed_calibration.get("stopping_policy") != stopping_policy:
            raise ValueError("calibration and evaluation stopping policies differ")
        calibration = fixed_calibration
    (output / "calibration.json").write_text(json.dumps(calibration, indent=2))
    evaluation = records if fixed_calibration is not None else [r for r in records if len(r["truth"]) > 1]
    frame = score(evaluation, thresholds)
    frame.to_csv(output / "summary.csv", index=False)
    groups = ["method", "drop", "keep_resource", "graph_mode", "n_true", "calibrated"]
    measurements = ["base_F1", *TOP_K_COLUMNS, "corca_P", "corca_R", "corca_F1", "corca_exact", "seconds",
                    "execution_success_rate", "failure_rate", "timeout_rate", "insufficient_evidence_rate"]
    if frame.empty:
        aggregate = pd.DataFrame(columns=groups + measurements + ["n"])
        aggregate.to_csv(output / "aggregate.csv", index=False)
    else:
        aggregate = frame.groupby(groups)[measurements].mean()
        aggregate["n"] = frame.groupby(groups).size()
        aggregate.to_csv(output / "aggregate.csv")
    report = {
        "attempted": len(records), "expected": expected,
        "missing": None if expected is None else expected - len(records),
        "status_counts": {m: dict(Counter(r["status"] for r in records if r["method"] == m)) for m in methods},
        "diagnosis_status_counts": {m: (frame.loc[frame["method"] == m, "diagnosis_status"].value_counts().to_dict()
                                         if not frame.empty else {}) for m in methods},
        "calibration": calibration,
        "calibration_mode": "fixed_separate_controls" if fixed_calibration is not None else "fit_single_fault_controls",
        "feature_policies": policies,
        "record_stopping_policy": stopping_policy,
        "scoring_stopping_policy": STOPPING_POLICY,
        "failure_policy": "all attempts included; missing calibration yields empty predictions scored zero, not measured algorithm accuracy",
        "baseline_policy": "base_F1/oracle_k_* receive the oracle root count; topKset_* return a fixed "
                           f"top-k set for k in {list(TOP_K_BASELINES)} without knowing the root count",
        "timing_policy": "includes worker startup and up to 3 rounds with tau=None; not deployed early-stop latency",
        "sample_policy": "dropout seeds reuse incidents; they are not independent incident repeats",
        "limitations": ["incident coverage and statistical power must be reviewed before generalization claims",
                        "confidence is an anomaly-score ratio, not a calibrated probability"],
        "failures": [{k: r[k] for k in ("case", "method", "drop", "seed", "status", "error_stage", "error") if k in r}
                     for r in records if r["status"] != "ok"],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    reliability = []
    for rec in evaluation:
        tau = thresholds.get(rec["method"])
        if tau is None:
            continue
        for round_ in rec["rounds"][:len(predicted_set(rec, tau))]:
            reliability.append({"method": rec["method"], "drop": rec["drop"],
                                "confidence": round_["confidence"], "correct": int(round_["service"] in rec["truth"])})
    if reliability:
        confidence = pd.DataFrame(reliability)
        confidence["bin"] = pd.cut(confidence["confidence"], [0, .2, .4, .6, .8, 1], include_lowest=True)
        confidence.groupby(["method", "drop", "bin"], observed=True).agg(
            mean_confidence=("confidence", "mean"), observed_accuracy=("correct", "mean"), n=("correct", "size")).to_csv(output / "confidence.csv")
    print(aggregate.round(3).to_string())
    print("Execution status counts:", json.dumps(report["status_counts"]))
    print(f"Evaluation complete: {output}. Successful execution alone does not establish generalization.")
    return report


def experiment_config(args, folders):
    package = metadata.distribution("RCAEval")
    return {"schema_version": 2, "data": str(Path(args.data).resolve()),
            "methods": args.methods, "rates": args.rates, "seeds": args.seeds,
            "timeout": args.timeout, "workers": args.workers,
            "stopping_policy": STOPPING_POLICY,
            "feature_policies": {m: FEATURE_POLICY if m == "circa" else "rcaeval-baro-v1" for m in args.methods},
            "calibration": None if not args.calibration else {
                "path": str(Path(args.calibration).resolve()),
                "sha256": hashlib.sha256(Path(args.calibration).read_bytes()).hexdigest()},
            "packages": {d.metadata["Name"]: d.version for d in metadata.distributions()},
            "ranker_sources": {str(p): hashlib.sha256(package.locate_file(p).read_bytes()).hexdigest()
                               for p in package.files or [] if str(p).startswith("RCAEval/") and str(p).endswith(".py")},
            "inputs": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for folder in folders
                       for p in [*folder.glob("*.json"), folder / "inject_time.txt"]},
            "metrics": {str(p / "metrics.csv"): hashlib.sha256((p / "metrics.csv").read_bytes()).hexdigest() for p in folders},
            "source": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                       for base in [Path("corca"), Path("runtime"), Path("scripts")]
                       for p in base.glob("*.py")}}


def validate_fixed_calibration(calibration, methods, metric_hashes):
    thresholds = calibration_thresholds(calibration)
    if not set(methods) <= thresholds.keys():
        raise ValueError("fixed calibration is missing a requested method")
    validate_calibration_split(calibration, metric_hashes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--data")
    inputs.add_argument("--diagnoses", help="audit existing diagnoses into a separate output, without rerunning rankers")
    parser.add_argument("--out", default="results/local-e2e")
    parser.add_argument("--methods", nargs="+", choices=["baro", "circa"], default=["baro", "circa"])
    parser.add_argument("--rates", nargs="+", type=float, default=[0, .1, .25, .5])
    parser.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--calibration", help="freeze thresholds from separate fingerprinted controls; never refit on test cases")
    args = parser.parse_args()
    if (not 1 <= args.workers <= 4 or args.timeout <= 0 or not math.isfinite(args.timeout)
            or any(not 0 <= r < 1 for r in args.rates) or any(not 0 <= s < 2**32 for s in args.seeds)):
        parser.error("invalid workers, timeout, or drop rates")
    output = Path(args.out)
    if args.diagnoses:
        if args.calibration:
            parser.error("--calibration requires --data; audit mode preserves historical calibration behavior")
        if output.exists():
            parser.error("audits require a new --out folder to preserve existing results")
        records = [json.loads(line) for line in Path(args.diagnoses).read_text().splitlines()]
        if not records:
            parser.error("no diagnosis records to audit")
        output.mkdir(parents=True)
        (output / "audit.json").write_text(json.dumps({"diagnoses": str(Path(args.diagnoses).resolve()),
            "sha256": hashlib.sha256(Path(args.diagnoses).read_bytes()).hexdigest(),
            "note": "recomputed report only; diagnosis records were produced by the original experiment"}, indent=2))
        write_reports(output, records, sorted({r["method"] for r in records}))
        return
    folders = sorted(p.parent for p in Path(args.data).glob("*/*/collection.json")
                     if json.loads(p.read_text()).get("status") == "complete")
    if not folders:
        parser.error("no complete, recovered incidents")
    output.mkdir(parents=True, exist_ok=True)
    config = experiment_config(args, folders)
    fixed = json.loads(Path(args.calibration).read_text()) if args.calibration else None
    if fixed is not None:
        try:
            validate_fixed_calibration(fixed, args.methods, config["metrics"])
            if any(fixed.get("feature_policies", {}).get(m) != config["feature_policies"][m] for m in args.methods):
                raise ValueError("calibration and evaluation feature policies differ")
        except (ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    manifest = output / "evaluation.json"
    if manifest.exists() and json.loads(manifest.read_text()) != config:
        parser.error("evaluation inputs or configuration changed; use a new --out folder")
    if not manifest.exists() and (output / "diagnoses.jsonl").exists():
        parser.error("existing diagnoses have no evaluation fingerprint; use a new --out folder")
    manifest.write_text(json.dumps(config, indent=2))
    path = output / "diagnoses.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    key = lambda r: (r["case"], r["method"], r["drop"], r["seed"], r["keep_resource"], r["graph_mode"])
    done = {key(r) for r in records}
    tasks, expected = [], set()
    for folder in folders:
        single = len(json.loads((folder / "ground_truth.json").read_text())["root_causes"]) == 1
        variants = [(0, 0, True, "observed")]
        if not single:
            variants += [(rate, seed, True, "observed") for rate in args.rates if rate > 0 for seed in args.seeds]
            variants += [(0, 0, False, "observed"), (0, 0, True, "static")]
        for method in args.methods:
            for rate, seed, keep, graph in variants:
                identity = (str(folder), method, rate, seed, keep, graph)
                expected.add(identity)
                if identity not in done:
                    tasks.append((folder, method, rate, seed, args.timeout, keep, graph))
    if len(done) != len(records) or not done <= expected:
        parser.error("duplicate or unexpected diagnosis records; preserve these results and use a new --out folder")
    with path.open("a") as stream, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate, *task) for task in tasks]
        for index, future in enumerate(as_completed(futures), 1):
            record = future.result()
            records.append(record)
            stream.write(json.dumps(record, allow_nan=False) + "\n")
            stream.flush()
            print(f"[{index}/{len(tasks)}] {record['method']} {record['case']} drop={record['drop']} {record['status']}", flush=True)
    if experiment_config(args, folders) != config:
        parser.error("inputs, code, or environment changed during evaluation; raw records preserved, results invalid")
    training_metrics = {str(f / "metrics.csv"): config["metrics"][str(f / "metrics.csv")] for f in folders
                        if len(json.loads((f / "ground_truth.json").read_text())["root_causes"]) == 1}
    write_reports(output, records, args.methods, expected=len(expected), fixed_calibration=fixed,
                  training_metrics=training_metrics)


if __name__ == "__main__":
    main()
