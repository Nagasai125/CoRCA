"""Measure bounded healthy-lab stability without injecting any faults."""
import argparse
import hashlib
import json
import math
import time
from pathlib import Path

from lab.run_cases import healthy
from lab.setup import LAB
from lab.telemetry import kube, spans_from_records, trace_snapshot


def snapshot():
    healthy()
    if json.loads(kube("-n", "corca", "get", "stresschaos,networkchaos", "-o", "json"))["items"]:
        raise ValueError("healthy soak requires no active chaos experiments")
    pods = json.loads(kube("-n", "corca", "get", "pods", "-l", "app=currencyservice", "-o", "json"))["items"]
    pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    if len(pods) != 1:
        raise ValueError("currencyservice must have exactly one stable pod")
    pod = pods[0]
    status = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "server")
    current, limit = map(int, kube("-n", "corca", "exec", pod["metadata"]["name"], "--", "cat",
                                  "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max").split())
    nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    if any(not any(c["type"] == "MemoryPressure" and c["status"] == "False"
                   for c in n["status"]["conditions"]) for n in nodes):
        raise ValueError("node memory pressure must be explicitly false")
    deployment = json.loads(kube("-n", "corca", "get", "deployment/currencyservice", "-o", "json"))
    template = deployment["spec"]["template"]
    return {"time": time.time(), "pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
            "container_id": status["containerID"], "restarts": status["restartCount"],
            "memory_bytes": current, "limit_bytes": limit,
            "template_sha256": hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()}


def check_sample(sample, first, max_fraction):
    if any(sample[k] != first[k] for k in ("uid", "container_id", "restarts", "template_sha256", "limit_bytes")):
        raise ValueError("pod, container, restart count, or deployment changed during the soak")
    if sample["limit_bytes"] <= 0 or sample["memory_bytes"] >= sample["limit_bytes"] * max_fraction:
        raise ValueError("currencyservice memory exceeded the predeclared headroom gate")


def run(output, seconds, interval, max_fraction):
    output.mkdir(parents=True, exist_ok=False)
    first, samples = None, []
    started, last = time.time(), time.time()
    policy = LAB / "kustomization.yaml"
    policy_hash = hashlib.sha256(policy.read_bytes()).hexdigest()
    report = {"status": "running", "scope": "bounded_healthy_soak_not_production_validation",
              "requested_seconds": seconds, "interval_seconds": interval,
              "max_memory_fraction": max_fraction, "started_at": started,
              "kustomization_sha256": policy_hash}
    path = output / "report.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    try:
        with (output / "samples.jsonl").open("x") as stream:
            while True:
                if time.time() - last > interval + 30:
                    raise ValueError("sampling interrupted by host suspension, overload, or a clock jump")
                sample = snapshot()
                first = sample if first is None else first
                check_sample(sample, first, max_fraction)
                samples.append(sample)
                stream.write(json.dumps(sample) + "\n")
                stream.flush()
                last = time.time()
                if last - started >= seconds:
                    break
                time.sleep(min(interval, seconds - (last - started)))
        if hashlib.sha256(policy.read_bytes()).hexdigest() != policy_hash:
            raise ValueError("lab configuration changed during the soak")
        traces = trace_snapshot(started, time.time())
        count = sum(service == "currencyservice" for service, _ in spans_from_records(traces))
        if not count:
            raise ValueError("no currencyservice trace spans observed; active traced workload is required")
        report.update(status="passed", currencyservice_spans=count)
    except BaseException as exc:
        report.update(status="failed", error=repr(exc))
        raise
    finally:
        elapsed = time.time() - started
        report.update(elapsed_seconds=elapsed, samples=len(samples),
                      long_soak_gate_met=report["status"] == "passed" and seconds >= 3600,
                      max_memory_bytes=max((s["memory_bytes"] for s in samples), default=None),
                      first=first, last=samples[-1] if samples else None)
        path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=3600)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--max-memory-fraction", type=float, default=.9)
    args = parser.parse_args()
    if (not all(math.isfinite(v) and v > 0 for v in (args.seconds, args.interval))
            or not 0 < args.max_memory_fraction < 1):
        parser.error("duration/interval must be finite and positive; memory fraction must be between zero and one")
    run(args.out, args.seconds, args.interval, args.max_memory_fraction)


if __name__ == "__main__":
    main()
