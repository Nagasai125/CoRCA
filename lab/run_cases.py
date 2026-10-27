"""Inject, verify, collect, recover, and label real local incidents."""
import argparse
import datetime as dt
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from lab.setup import LAB
from lab.telemetry import SERVICES, collect, kube, prometheus, trace_snapshot


def fault_object(root, name, duration):
    spec = {"mode": "all", "selector": {"namespaces": ["corca"], "labelSelectors": {"app": root["service"]}},
            "duration": f"{duration}s"}
    if root["fault"] in ("cpu", "mem"):
        kind = "StressChaos"
        spec["stressors"] = {"cpu": {"workers": 1, "load": 80}} if root["fault"] == "cpu" else {"memory": {"workers": 1, "size": "50MiB"}}
    else:
        kind = "NetworkChaos"
        spec.update(action="delay", mode="all", direction="to", delay={"latency": "100ms", "jitter": "10ms"})
    return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": kind,
            "metadata": {"name": name, "namespace": "corca", "labels": {"app.kubernetes.io/part-of": "corca-lab"}},
            "spec": spec}


def status(obj):
    return json.loads(kube("-n", "corca", "get", obj["kind"], obj["metadata"]["name"], "-o", "json"))


def wait_condition(objects, condition, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = [status(obj) for obj in objects]
        if all(any(c["type"] == condition and c["status"] == "True" for c in r.get("status", {}).get("conditions", [])) for r in records):
            return records
        time.sleep(1)
    raise TimeoutError(f"faults did not reach {condition} within {timeout}s")


def apply_times(records):
    timestamps = []
    for record in records:
        events = [e for container in record["status"]["experiment"]["containerRecords"] for e in container.get("events", [])
                  if e["type"] == "Succeeded" and e["operation"] == "Apply"]
        if not events:
            raise ValueError("controller did not record a successful injection timestamp")
        timestamps.extend(dt.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() for e in events)
    return min(timestamps), max(timestamps)


def healthy():
    pods = json.loads(kube("-n", "corca", "get", "pods", "-o", "json"))["items"]
    apps = {p["metadata"].get("labels", {}).get("app"): p for p in pods
            if not p["metadata"].get("deletionTimestamp")}
    required = {"redis-cart" if s == "redis" else s for s in SERVICES} | {"loadgenerator", "prometheus", "otel-collector"}
    if not required <= apps.keys() or any(not any(c["type"] == "Ready" and c["status"] == "True"
                                                for c in apps[s].get("status", {}).get("conditions", [])) for s in required):
        raise ValueError("application, load, and telemetry pods must all be ready before injection")
    return {s: sum(c.get("restartCount", 0) for c in apps[s].get("status", {}).get("containerStatuses", [])) for s in required}


def run_case(scenario, repeat, output, normal_seconds, fault_seconds):
    folder = output / scenario["name"] / str(repeat)
    if (folder / "collection.json").exists():
        previous = json.loads((folder / "collection.json").read_text())
        if previous.get("status") == "complete":
            print(f"Already collected: {folder}", flush=True)
            return
        raise ValueError(f"incomplete case preserved at {folder}; use a new output folder")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "deployment-config.json").write_text(kube("-n", "corca", "get", "deployments", "-o", "json"))
    (folder / "pods-before.json").write_text(kube("-n", "corca", "get", "pods", "-o", "json"))
    before = healthy()
    active = json.loads(kube("-n", "corca", "get", "stresschaos,networkchaos", "-o", "json"))["items"]
    if active:
        raise ValueError("existing chaos experiments must be recovered before starting a case")
    snapshot = prometheus('container_memory_working_set_bytes{namespace="corca",container!="",container!="POD"}')
    if not snapshot["data"]["result"] or not trace_snapshot(time.time() - 60, time.time()):
        raise ValueError("resource metrics and traces must be flowing before injection")
    print("Waiting 60s for recovery and CPU counter history before the normal window", flush=True)
    time.sleep(60)
    before = healthy()
    record = {"status": "collecting", "scenario": scenario, "repeat": repeat,
              "normal_seconds": normal_seconds, "fault_seconds": fault_seconds,
              "cpu_rate": "irate_60s", "fault_settle_seconds": 30,
              "cluster": "corca-lab", "namespace": "corca", "start": time.time()}
    report = folder / "collection.json"
    report.write_text(json.dumps(record, indent=2))
    tag = "corca-" + uuid.uuid4().hex[:12]
    objects = [fault_object(root, tag + "-" + str(i), fault_seconds + 180) for i, root in enumerate(scenario["roots"])]
    (folder / "faults.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2))
    applied = False
    try:
        print(f"Normal window: {folder} ({normal_seconds}s)", flush=True)
        time.sleep(normal_seconds)
        if time.time() - record["start"] > normal_seconds + 30:
            raise ValueError("normal window interrupted by host suspension or a clock jump")
        if healthy() != before:
            raise ValueError("containers restarted during the normal window; reject contaminated baseline")
        applied = True
        kube("create", "-f", "-", payload={"apiVersion": "v1", "kind": "List", "items": objects})
        injected = wait_condition(objects, "AllInjected")
        first, last = apply_times(injected)
        (folder / "injected-status.json").write_text(json.dumps(injected, indent=2))
        record.update(first_injected=first, all_injected=last)
        print(f"All {len(objects)} roots injected: {scenario['name']}; collecting {fault_seconds}s", flush=True)
        time.sleep(fault_seconds)
        if time.time() - last > fault_seconds + 30:
            raise ValueError("fault window interrupted by host suspension or a clock jump")
        ending = [status(obj) for obj in objects]
        if not all(any(c["type"] == "AllInjected" and c["status"] == "True" for c in r["status"]["conditions"]) for r in ending):
            raise ValueError("faults did not remain active for the collection window")
        end = time.time()
        record["end"] = end
        (folder / "ending-status.json").write_text(json.dumps(ending, indent=2))
        collect(folder, record["start"], first, last, end)
        (folder / "ground_truth.json").write_text(json.dumps({"root_causes": [r["service"] for r in scenario["roots"]],
                                                             "faults": [r["fault"] for r in scenario["roots"]]}, indent=2))
        record["status"] = "collected"
    except BaseException as exc:
        record.update(status="failed", error=repr(exc))
        raise
    finally:
        if applied:
            try:
                for obj in objects:
                    kube("-n", "corca", "annotate", obj["kind"], obj["metadata"]["name"],
                         "experiment.chaos-mesh.org/pause=true", "--overwrite")
                recovered = wait_condition(objects, "AllRecovered", timeout=90)
                (folder / "recovered-status.json").write_text(json.dumps(recovered, indent=2))
                for obj in objects:
                    kube("-n", "corca", "delete", obj["kind"], obj["metadata"]["name"], "--timeout=30s", timeout=40)
                deadline = time.monotonic() + 90
                while True:
                    try:
                        after = healthy()
                        break
                    except ValueError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(2)
                record["restarts_before"] = before
                record["restarts_after"] = after
                record["recovered_at"] = time.time()
                if record["status"] == "collected":
                    record["status"] = "complete"
            except BaseException as exc:
                record.update(status="failed", recovery_error=repr(exc))
                report.write_text(json.dumps(record, indent=2))
                raise
        report.write_text(json.dumps(record, indent=2))
    print(f"Collected and recovered: {folder}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=str(LAB / "cases.json"))
    parser.add_argument("--out", default="data/local")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--normal-seconds", type=int, default=300)
    parser.add_argument("--fault-seconds", type=int, default=300)
    parser.add_argument("--only", nargs="+", help="scenario names")
    args = parser.parse_args()
    if args.repeats < 1 or args.normal_seconds < 75 or args.fault_seconds < 120:
        parser.error("repeats must be positive; normal window >=75s and fault window >=120s")
    scenarios = json.loads(Path(args.plan).read_text())
    for scenario in scenarios:
        if not re.fullmatch(r"[a-z0-9_+-]+", scenario["name"]) or not 1 <= len(scenario["roots"]) <= 3:
            parser.error("invalid scenario name or root count")
        roots = scenario["roots"]
        if len({r["service"] for r in roots}) != len(roots) or any(r["service"] not in SERVICES or r["fault"] not in ("cpu", "mem", "delay") for r in roots):
            parser.error("invalid root services or fault types")
    if args.only and not set(args.only) <= {s["name"] for s in scenarios}:
        parser.error("unknown scenario selected")
    scenarios = [s for s in scenarios if not args.only or s["name"] in args.only]
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    config = {"scenarios": scenarios, "repeats": args.repeats,
              "normal_seconds": args.normal_seconds, "fault_seconds": args.fault_seconds,
              "sources": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in LAB.iterdir()
                          if p.suffix in (".py", ".yaml", ".json")}}
    manifest = output / "experiment.json"
    if manifest.exists() and json.loads(manifest.read_text()) != config:
        parser.error("collection configuration or source changed; use a new --out folder")
    manifest.write_text(json.dumps(config, indent=2))
    failed = []
    for scenario in scenarios:
        for repeat in range(1, args.repeats + 1):
            for attempt in (1, 2):  # one retry per incident
                try:
                    run_case(scenario, repeat, output, args.normal_seconds, args.fault_seconds)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"FAILED {scenario['name']}/{repeat} attempt {attempt}: {exc!r}", flush=True)
                    # Move the rejected incident out of the dataset (kept for the report), so it can be re-run.
                    folder = output / scenario["name"] / str(repeat)
                    if folder.exists():
                        dest = output.parent / (output.name + "-rejected") / scenario["name"] / f"{repeat}-attempt{attempt}-{int(time.time())}"
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        folder.rename(dest)
                    # If chaos could not be cleaned up, every later case would fail: stop instead.
                    if json.loads(kube("-n", "corca", "get", "stresschaos,networkchaos", "-o", "json"))["items"]:
                        raise SystemExit("chaos experiments still active after a failure; stopping") from exc
                    if attempt == 2:
                        failed.append(f"{scenario['name']}/{repeat}")
                    else:
                        time.sleep(120)  # let restarted pods settle before retrying
    print(f"Done. {len(failed)} incident(s) failed twice: {failed}", flush=True)


if __name__ == "__main__":
    main()
