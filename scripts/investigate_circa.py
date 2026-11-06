"""Trace local PC/RHT failures and compare an experimental residual-noise floor.

Development only: the candidate is not registered as a production RCA method.
"""
import argparse
import json
import math
import os
import random
import time
from importlib import metadata
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
from RCAEval.graph_heads.rht import ANMRegressor, RHTScorer

from corca import corca
from corca.base import anomaly_scores, services_from_metrics
from corca.circa import FEATURE_POLICY, InsufficientEvidence, rank_circa
from corca.data import list_cases, load_case
from corca.execution import call_with_timeout
from corca.metrics import exact_match, set_prf
from corca.topology import call_graph
from scripts.experiment import digest_file
from scripts.summarize import calibrate, predicted_set, validate_calibration_split
from scripts.structural_circa import STRUCTURAL_POLICIES, StrictANMRegressor, rank_structural


POLICIES = (FEATURE_POLICY, "experimental-rht-residual-floor-1pct-v1")


def measurement_floor(train_y, metric):
    absolute = {"error": .01, "log-error": 1.0, "workload": 1.0}.get(metric, 1e-9)
    return max(absolute, .01 * abs(float(np.median(train_y))))


class FloorRegressor(ANMRegressor):
    def __init__(self, metric):
        super().__init__()
        self.metric = metric

    def score(self, train_x, test_x, train_y, test_y):
        if len(train_x):
            self._regressor.fit(train_x, train_y)
            train_error = train_y - self._regressor.predict(train_x)
            test_error = test_y - self._regressor.predict(test_x)
        else:
            train_error, test_error = train_y, test_y
        sigma = max(float(np.std(train_error)), measurement_floor(train_y, self.metric))
        return (test_error - np.mean(train_error)) / sigma


class TraceScorer(RHTScorer):
    def __init__(self, floor=False, **kwargs):
        super().__init__(**kwargs)
        self.floor, self.details = floor, []

    def score_node(self, graph, series, node, data):
        if self.floor:
            self._regressor = FloorRegressor(node.metric)
        result = super().score_node(graph, series, node, data)
        parents = list(graph.parents(node))
        train_x, test_x, train_y, test_y = self.split_data(series, node, parents, data)
        detail = {"metric": f"{node.entity}_{node.metric}", "score": float(result.score),
                  "parents": sorted(f"{p.entity}_{p.metric}" for p in parents),
                  "normal_metric_std": float(np.std(train_y)),
                  "normal_metric_median": float(np.median(train_y)),
                  "candidate_scale_floor": measurement_floor(train_y, node.metric)}
        try:
            if len(train_x):
                model = self._regressor._regressor
                train_error = train_y - model.predict(train_x)
                test_error = test_y - model.predict(test_x)
            else:
                train_error, test_error = train_y, test_y
            std = float(np.std(train_error))
            detail["normal_residual_std"] = std
            if std > 0:
                z = np.abs((test_error - np.mean(train_error)) / std)
                detail.update(residual_z_median=float(np.median(z)),
                              residual_z_q90=float(np.quantile(z, .9)), residual_z_max=float(np.max(z)))
        except ValueError as exc:
            detail["regression_diagnostics_error"] = str(exc)
        self.details.append(detail)
        return result


def truth_for(folder):
    cases = list_cases(str(folder))
    if len(cases) != 1 or Path(cases[0].path).resolve() != Path(folder).resolve():
        raise ValueError("each path must identify one incident folder")
    return cases[0].root_causes


def analyze(folder, policy, dataset):
    random.seed(0)
    np.random.seed(0)
    if dataset == "re2-ob":
        from backend.api import Incident, prepare_window
        from lab.client import incident_payload

        incident = Incident(**incident_payload(folder, method="circa"))
        df, dropped, _ = prepare_window(incident)
        inject_time, graph = incident.inject_time, nx.DiGraph(incident.edges)
    else:
        df, inject_time = load_case(str(folder))
        dropped, graph = [], call_graph()
    truth = truth_for(folder)
    traces = []

    def rank(frame, inject_time):
        structural = policy in STRUCTURAL_POLICIES
        scorer = TraceScorer(regressor=StrictANMRegressor()) if structural else TraceScorer(floor=policy == POLICIES[1])
        metrics = (rank_structural(frame, inject_time, dataset, graph,
                                   adjustment=policy == STRUCTURAL_POLICIES[1], scorer=scorer)
                   if structural else rank_circa(frame, inject_time, dataset, scorer=scorer))
        traces.append({"ranked_services": services_from_metrics(metrics),
                       "metrics": sorted(scorer.details, key=lambda x: (-x["score"], x["metric"]))})
        return metrics

    result = corca(df, inject_time, rank_fn=rank, graph=graph,
                   max_rounds=3, tau=None)
    normal = int((df.time < inject_time).sum())
    return {"case": str(folder), "method": policy, "feature_policy": policy, "truth": truth,
            "status": "ok", "normal_samples": normal, "fault_samples": len(df) - normal,
            "dataset": dataset,
            "dropped_metrics": dropped, "base_rank": traces[0]["ranked_services"] if traces else [],
            "rounds": [vars(r) for r in result.rounds], "traces": traces,
            "marginal_anomaly_scores": {k: float(v) for k, v in anomaly_scores(df, inject_time).items()}}


def run(folder, policy, timeout, dataset):
    started = time.perf_counter()
    try:
        record = call_with_timeout(analyze, timeout, (str(folder), policy, dataset))
    except Exception as exc:
        record = {"case": str(folder), "method": policy, "status": "insufficient_evidence"
                  if isinstance(exc, InsufficientEvidence) else "timeout" if isinstance(exc, TimeoutError) else "error",
                  "error": str(exc), "truth": [], "base_rank": [], "rounds": []}
    record["seconds"] = time.perf_counter() - started
    return record


def input_hashes(folders):
    names = ("data.csv", "metrics.csv", "inject_time.txt", "ground_truth.json", "topology.json", "collection.json")
    return {str(folder / name): digest_file(folder / name) for folder in folders for name in names
            if (folder / name).is_file()}


def metric_hash(folder):
    local = (folder / "collection.json").exists()
    return digest_file(folder / ("data.csv" if not local and (folder / "data.csv").exists() else "metrics.csv"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", nargs="+", required=True, type=Path)
    parser.add_argument("--case", nargs="+", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--dataset", choices=("re1-ob", "re2-ob"), default="re2-ob")
    parser.add_argument("--policies", nargs="+", choices=POLICIES + STRUCTURAL_POLICIES, default=POLICIES)
    parser.add_argument("--timeout", type=float, default=60, help="seconds for one whole three-round case")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    controls = list(dict.fromkeys(p.resolve() for p in args.control))
    cases = list(dict.fromkeys(p.resolve() for p in args.case))
    folders = list(dict.fromkeys(controls + cases))
    try:
        if any((folder / "collection.json").exists() != (args.dataset == "re2-ob") for folder in folders):
            raise ValueError("use re2-ob for collected local incidents or re1-ob for public cases; do not mix datasets")
        hashes = input_hashes(folders)
        for folder in controls:
            if len(truth_for(folder)) != 1:
                raise ValueError("calibration controls must contain exactly one root")
        validate_calibration_split(
            {"training_metrics_sha256": {str(p): metric_hash(p) for p in controls}},
            {str(p): metric_hash(p) for p in cases})
        args.out.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    source = [p for base in ("corca", "scripts", "backend", "lab") for p in Path(base).glob("*.py")]
    package = metadata.distribution("RCAEval")
    policies = list(dict.fromkeys(args.policies))
    manifest = {"scope": "development_only_not_held_out", "policies": policies,
                "dataset": args.dataset, "base_feature_policy": FEATURE_POLICY,
                "controls": list(map(str, controls)), "cases": list(map(str, cases)), "timeout": args.timeout,
                "environment": {key: os.environ.get(key) for key in
                                ("PYTHONHASHSEED", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")},
                "inputs": hashes, "source": {str(p): digest_file(p) for p in source},
                "ranker_sources": {str(p): digest_file(package.locate_file(p)) for p in package.files or []
                                   if str(p).startswith("RCAEval/") and str(p).endswith(".py")},
                "packages": {d.metadata["Name"]: d.version for d in metadata.distributions()}}
    (args.out / "experiment.json").write_text(json.dumps(manifest, indent=2))
    records = []
    with (args.out / "diagnoses.jsonl").open("x") as stream:
        for policy in policies:
            for folder in folders:
                record = run(folder, policy, args.timeout, args.dataset)
                record["split"] = "control" if folder in controls else "development"
                records.append(record)
                stream.write(json.dumps(record, allow_nan=False) + "\n")
                stream.flush()
                print(policy, folder.name, record["status"], flush=True)
    if input_hashes(folders) != hashes or any(digest_file(p) != v for p, v in manifest["source"].items()):
        parser.error("inputs or code changed during analysis; raw records preserved, comparison invalid")
    taus, rows, unavailable = {}, [], {}
    for policy in policies:
        try:
            taus[policy] = calibrate([r for r in records if r["method"] == policy and r["split"] == "control"], .9)
        except ValueError as exc:
            unavailable[policy] = str(exc)
        for record in records:
            if record["method"] != policy or record["split"] != "development":
                continue
            truth = truth_for(Path(record["case"]))
            pred = predicted_set(record, taus[policy]) if policy in taus and record["status"] == "ok" else []
            p, r, f = set_prf(pred, truth)
            rows.append({"case": record["case"], "policy": policy, "status": record["status"],
                         "predicted": json.dumps(pred), "precision": p, "recall": r, "F1": f,
                         "exact": exact_match(pred, truth), "tau": taus.get(policy)})
    pd.DataFrame(rows).to_csv(args.out / "comparison.csv", index=False)
    (args.out / "report.json").write_text(json.dumps({"scope": manifest["scope"], "thresholds": taus,
        "unavailable": unavailable, "comparisons": rows,
        "limitations": ["existing incidents used for development, not untouched test data",
                        "noise floor is a heuristic and can hide real sub-percent memory faults",
                        "structural mapping and linear RHT are experimental, not a full original CIRCA reproduction",
                        "candidate is not registered in the API or production evaluator"]}, indent=2))
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
