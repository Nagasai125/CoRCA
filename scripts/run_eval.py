"""Run bounded, reproducible baseline and CoRCA evaluations."""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path

from corca import corca, list_cases, load_case
from corca.base import rank_metrics, services_from_metrics
from corca.circa import FEATURE_POLICY, InsufficientEvidence
from corca.corca import STOPPING_POLICY
from corca.execution import call_with_timeout
from corca.synthetic import drop_telemetry
from scripts.experiment import prepare_experiment, read_runs


def positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def evaluate_case(case, args, experiment_id):
    record = {
        "experiment_id": experiment_id,
        "case": str(Path(case.path).resolve().relative_to(Path(args.data).resolve())),
        "method": args.method, "truth": case.root_causes, "faults": case.faults,
        "case_id": case.case_id, "base_rank": [], "rounds": [],
        "status": "error", "timed_out": False,
        "baseline_seconds": 0.0, "corca_seconds": 0.0,
        "drop_telemetry": args.drop_telemetry, "seed": args.seed,
        "feature_policy": FEATURE_POLICY if args.method == "circa" else "rcaeval-baro-v1",
        "stopping_policy": STOPPING_POLICY,
    }
    started = time.perf_counter()
    stage = "load"
    stage_started = started
    try:
        df, inject_time = load_case(case.path)
        record["metrics_before_drop"] = len(df.columns) - 1
        # Never protect ground-truth services: missing root telemetry is part of the experiment.
        df = drop_telemetry(df, args.drop_telemetry, seed=args.seed)
        record["metrics_after_drop"] = len(df.columns) - 1
        rank = partial(rank_metrics, args.method, dataset=args.dataset, seed=args.seed)
        stage = "baseline"
        stage_started = time.perf_counter()
        initial_rank = call_with_timeout(rank, args.round_timeout, (df, inject_time))
        record["baseline_seconds"] = time.perf_counter() - stage_started
        record["base_rank"] = services_from_metrics(initial_rank)
        stage = "corca"
        stage_started = time.perf_counter()
        result = corca(df, inject_time, rank_fn=rank, initial_rank=initial_rank,
                       max_rounds=args.max_rounds, tau=None,
                       keep_resource=not args.no_keep_resource, dataset=args.dataset,
                       round_timeout=args.round_timeout)
        # A standalone CoRCA diagnosis pays for round 1, even though this evaluation reuses it.
        record["corca_seconds"] = record["baseline_seconds"] + time.perf_counter() - stage_started
        record["rounds"] = [r.__dict__ for r in result.rounds]
        record["timed_out"] = result.timed_out
        record["status"] = "timeout" if result.timed_out else "ok"
        if result.timed_out:
            record["error_stage"] = "corca"
    except Exception as exc:
        record["status"] = ("insufficient_evidence" if isinstance(exc, InsufficientEvidence) else
                            "timeout" if isinstance(exc, TimeoutError) else "error")
        record["timed_out"] = isinstance(exc, TimeoutError)
        record["error"] = str(exc)
        record["error_stage"] = stage
        if stage == "baseline":
            record["baseline_seconds"] = time.perf_counter() - stage_started
        elif stage == "corca":
            record["corca_seconds"] = record["baseline_seconds"] + time.perf_counter() - stage_started
    record["seconds"] = time.perf_counter() - started
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True)
    ap.add_argument("--methods", nargs="+", choices=["baro", "circa"], default=["baro"])
    ap.add_argument("--out", default="results")
    ap.add_argument("--max-rounds", type=positive_int, default=3)
    ap.add_argument("--dataset", default="re1-ob")
    ap.add_argument("--limit", type=positive_int, help="smoke test; may resume without this limit")
    ap.add_argument("--round-timeout", type=float, default=60,
                    help="seconds per baseline/CoRCA rank call, including worker startup")
    ap.add_argument("--no-keep-resource", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--drop-telemetry", type=float, default=0,
                    help="fraction of metric streams removed, including root services")
    ap.add_argument("--retry-failed", action="store_true", help="retry errors and partial timeouts")
    ap.add_argument("--workers", type=int, choices=range(1,5), default=1,
                    help="bounded concurrent case evaluations")
    args = ap.parse_args()
    if not 0 < args.round_timeout < float("inf"):
        ap.error("--round-timeout must be finite and positive")
    if not 0 <= args.drop_telemetry < 1:
        ap.error("--drop-telemetry must be in [0, 1)")
    if not 0 <= args.seed < 2**32:
        ap.error("--seed must be in [0, 2**32)")
    try:
        all_cases = list_cases(args.data)
        experiment_id = prepare_experiment(args, all_cases)
        output = Path(args.out) / "runs.jsonl"
        prior = read_runs(output, experiment_id) if output.exists() else []
    except (ValueError, FileNotFoundError) as exc:
        ap.error(str(exc))
    done = {(r["case"], r["method"]) for r in prior
            if not args.retry_failed or r["status"] == "ok"}
    cases = all_cases[:args.limit]
    print(f"{len(cases)} cases, methods={args.methods}, {len(done)} runs already done", flush=True)
    with output.open("a") as stream, ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks = []
        for case in cases:
            case_key = str(Path(case.path).resolve().relative_to(Path(args.data).resolve()))
            for method in dict.fromkeys(args.methods):
                if (case_key, method) in done:
                    continue
                task_args = argparse.Namespace(**vars(args), method=method)
                tasks.append(pool.submit(evaluate_case, case, task_args, experiment_id))
        for index, future in enumerate(as_completed(tasks), 1):
            record = future.result()
            stream.write(json.dumps(record, allow_nan=False) + "\n")
            stream.flush()
            print(f"[{index}/{len(tasks)}] {record['method']} {record['case']} {record['status']} "
                  f"base={record['base_rank'][:3]} "
                  f"corca={[r['service'] for r in record['rounds']]}", flush=True)
    if prepare_experiment(args, all_cases) != experiment_id:
        ap.error("experiment changed during evaluation; preserve records and use a new output folder")


if __name__ == "__main__":
    main()
