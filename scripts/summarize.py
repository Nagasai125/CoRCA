"""Score every attempted case, with held-out calibration and explicit failures."""
import argparse
import json
import math
from pathlib import Path

import pandas as pd

from corca.metrics import exact_match, set_prf, top_k_hit
from corca.corca import STOPPING_POLICY
from scripts.experiment import read_runs


def predicted_set(rec, tau):
    rounds = rec.get("rounds", [])
    if not rounds:
        return []
    pred = [rounds[0]["service"]]
    for prev, nxt in zip(rounds, rounds[1:]):
        if prev["residual"] <= tau:
            break
        pred.append(nxt["service"])
    return pred


def diagnosis_evidence(rec, tau):
    """Describe the calibrated stopping point, not later tau=None diagnostic rounds."""
    pred = predicted_set(rec, tau) if tau is not None else []
    residual = rec["rounds"][len(pred) - 1]["residual"] if pred else None
    status = rec.get("status", "error" if "error" in rec else
                     "timeout" if rec.get("timed_out") else "ok")
    if tau is None:
        status = "uncalibrated"
    elif status in ("ok", "timeout") and pred and residual <= tau:
        status = "diagnosed"
    elif status == "timeout":
        status = "partial_timeout"
    elif status == "ok":
        status = "partial_unexplained" if pred else "insufficient_evidence"
    return {"diagnosis_status": status, "remaining_anomaly_score": residual}


def calibration_residuals(recs):
    return [r["rounds"][0]["residual"] for r in recs
            if len(r["truth"]) == 1 and r.get("rounds")
            and r.get("status", "ok") == "ok"
            and r["rounds"][0]["service"] == r["truth"][0]]


def calibrate(recs, q):
    residuals = calibration_residuals(recs)
    if not residuals:
        raise ValueError("no correctly ranked single-fault training cases to calibrate tau")
    return float(pd.Series(residuals).quantile(q))


def result_policies(records):
    policies = {}
    for method in {r["method"] for r in records}:
        versions = {r.get("feature_policy", "legacy_unversioned") for r in records if r["method"] == method}
        if len(versions) != 1:
            raise ValueError(f"cannot pool different feature policies for {method}")
        policies[method] = versions.pop()
    stopping = {r.get("stopping_policy", "legacy_unversioned") for r in records}
    if len(stopping) != 1:
        raise ValueError("cannot pool different stopping policies")
    return {"feature_policies": policies, "stopping_policy": stopping.pop()}


def validate_calibration_split(calibration, metric_hashes):
    training = calibration.get("training_metrics_sha256")
    if not training:
        raise ValueError("fixed calibration requires training metric fingerprints to check incident overlap")
    if set(training.values()) & set(metric_hashes.values()):
        raise ValueError("evaluation contains calibration incidents; use new held-out incidents")


def summarize(recs, q=0.9, fixed_taus=None):
    if not recs:
        raise ValueError("no runs to summarize")
    rows, calibration = [], {}
    for method in sorted({r["method"] for r in recs}):
        method_recs = [r for r in recs if r["method"] == method]
        if fixed_taus is not None:
            if method not in fixed_taus:
                raise ValueError(f"missing fixed calibration for {method}")
            taus = {r["case_id"]: fixed_taus[method] for r in method_recs}
        else:
            if any(len(r["truth"]) != 1 for r in method_recs):
                raise ValueError("multi-fault runs require --calibration or --tau from separate single-fault data")
            taus = {}
            for case_id in {r["case_id"] for r in method_recs}:
                training = [r for r in method_recs if r["case_id"] != case_id]
                try:
                    taus[case_id] = calibrate(training, q)
                except ValueError as exc:
                    raise ValueError(f"{method}, held-out repeat {case_id}: {exc}") from exc
            calibration[method] = {"tau": calibrate(method_recs, q),
                                   "n_training": len(calibration_residuals(method_recs)),
                                   "fold_taus": taus}
        for rec in method_recs:
            tau = taus[rec["case_id"]]
            truth = rec["truth"]
            pred = predicted_set(rec, tau)
            precision, recall, f1 = set_prf(pred, truth)
            base_rank = rec.get("base_rank", [])
            # Oracle-count baseline: disclose that it is given the true number of roots.
            bp, br, bf = set_prf(base_rank[:len(truth)], truth)
            status = rec.get("status", "error" if "error" in rec else
                             "timeout" if rec.get("timed_out") else "ok")
            confidences = [r["confidence"] for r in rec.get("rounds", [])[:len(pred)]
                           if "confidence" in r]
            rows.append({
                **diagnosis_evidence(rec, tau),
                "case": rec["case"], "method": method, "case_id": rec["case_id"],
                "n_true": len(truth), "fault": "+".join(rec.get("faults", [])),
                "status": status, "error_stage": rec.get("error_stage", ""),
                "truth": json.dumps(truth), "predicted": json.dumps(pred), "tau": tau,
                "top1": top_k_hit(base_rank, truth, 1),
                "top3": top_k_hit(base_rank, truth, 3),
                "top5": top_k_hit(base_rank, truth, 5),
                "base_P": bp, "base_R": br, "base_F1": bf,
                "corca_P": precision, "corca_R": recall, "corca_F1": f1,
                "corca_exact": exact_match(pred, truth), "corca_n_pred": len(pred),
                "extra_roots": float(len(pred) > len(truth)),
                "corca_confidence": sum(confidences) / len(confidences) if confidences else float("nan"),
                "failed": float(status == "error"), "timed_out": float(status == "timeout"),
                "baseline_seconds": rec.get("baseline_seconds", float("nan")),
                "corca_seconds": rec.get("corca_seconds", float("nan")),
                "seconds": rec.get("seconds", float("nan")),
            })
    return pd.DataFrame(rows), calibration


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs")
    ap.add_argument("--q", type=float, default=0.9)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--tau", type=float, help="fixed tau shared by all methods")
    group.add_argument("--calibration", help="calibration.json from separate single-fault training data")
    args = ap.parse_args()
    if not 0 < args.q < 1:
        ap.error("--q must be in (0, 1)")
    if args.tau is not None and (not math.isfinite(args.tau) or args.tau < 0):
        ap.error("--tau must be finite and nonnegative")
    output = Path(args.runs).parent
    manifest_path = output / "experiment.json"
    try:
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        recs = read_runs(args.runs, manifest["experiment_id"] if manifest else None)
        policies = result_policies(recs)
        metric_hashes = {f"{case}/{name}": digest
                         for case, files in (manifest or {}).get("config", {}).get("inputs", {}).items()
                         if case in {r["case"] for r in recs}
                         for name, digest in files.items() if name in ("data.csv", "metrics.csv")}
        fixed = None
        if args.tau is not None:
            fixed = {r["method"]: args.tau for r in recs}
        elif args.calibration:
            training = json.loads(Path(args.calibration).read_text())
            fixed = {m: v["tau"] for m, v in training["methods"].items()}
            if any(not math.isfinite(v) or v < 0 for v in fixed.values()):
                raise ValueError("calibration thresholds must be finite and nonnegative")
            if manifest and training.get("experiment_id") == manifest["experiment_id"]:
                raise ValueError("fixed calibration must come from a separate training experiment")
            if (training.get("stopping_policy") != STOPPING_POLICY
                    or policies["stopping_policy"] != STOPPING_POLICY
                    or any(training.get("feature_policies", {}).get(m) != version
                           for m, version in policies["feature_policies"].items())):
                raise ValueError("fixed calibration policies changed or are missing; rerun and recalibrate")
            if not metric_hashes:
                raise ValueError("fixed calibration requires fingerprinted evaluation inputs")
            validate_calibration_split(training, metric_hashes)
        df, calibration = summarize(recs, args.q, fixed)
    except (ValueError, KeyError, OSError) as exc:
        ap.error(str(exc))
    if not manifest:
        print("WARNING: legacy results lack experiment fingerprints; timings and failure detection are unverified.")
    df.to_csv(output / "summary.csv", index=False)
    cols = ["top1", "top3", "top5", "base_F1", "corca_P", "corca_R", "corca_F1",
            "corca_exact", "corca_n_pred", "extra_roots", "failed", "timed_out",
            "baseline_seconds", "corca_seconds", "seconds"]
    aggregate = df.groupby(["method", "n_true"])[cols].mean()
    aggregate["n"] = df.groupby(["method", "n_true"]).size()
    for stage in ["baseline_seconds", "corca_seconds"]:
        aggregate[stage + "_p95"] = df.groupby(["method", "n_true"])[stage].quantile(0.95)
    aggregate.to_csv(output / "aggregate.csv")
    coverage = {}
    if manifest:
        expected = len(manifest["config"]["inputs"])
        for method in manifest["config"]["methods"]:
            attempted = sum(r["method"] == method for r in recs)
            coverage[method] = {"expected": expected, "attempted": attempted, "missing": expected - attempted}
    report = {"experiment_id": manifest["experiment_id"] if manifest else None,
              "coverage": coverage, "q": args.q,
              "calibration_mode": "fixed" if fixed is not None else "held_out_repeat",
              **policies,
              "scoring_stopping_policy": STOPPING_POLICY,
              "failure_policy": "all attempted cases included; unavailable predictions score zero",
              "timing_policy": "includes spawned-worker startup; CoRCA runs up to max_rounds with tau=None",
              "baseline_policy": "set baseline receives oracle root count",
              "status_counts": {m: g["status"].value_counts().to_dict() for m, g in df.groupby("method")},
              "diagnosis_status_counts": {m: g["diagnosis_status"].value_counts().to_dict()
                                          for m, g in df.groupby("method")}}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if calibration:
        (output / "calibration.json").write_text(json.dumps({"experiment_id": report["experiment_id"],
                                                            "q": args.q, "methods": calibration,
                                                            "training_metrics_sha256": metric_hashes,
                                                            **policies}, indent=2) + "\n")
    print("Scores include failed and timed-out attempts. Missing cases are shown in coverage.")
    print(json.dumps(coverage))
    print(aggregate.round(3).to_string())
    print(f"Per-case scores and experiment report: {output}")


if __name__ == "__main__":
    main()
