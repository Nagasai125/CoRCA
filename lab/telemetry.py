"""Collect real resource metrics, traces, and logs into RCAEval-format cases."""
import datetime as dt
import json
import subprocess
import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd

from corca.topology import call_graph, service_of
from lab.setup import KUBECONFIG


SERVICES = set(call_graph()) - {"frontend-external"}
RPC_SERVICES = {"CurrencyService": "currencyservice", "CartService": "cartservice",
                "CheckoutService": "checkoutservice", "AdService": "adservice",
                "ProductCatalogService": "productcatalogservice", "RecommendationService": "recommendationservice",
                "ShippingService": "shippingservice", "PaymentService": "paymentservice", "EmailService": "emailservice"}


def kube(*args, payload=None, timeout=30):
    result = subprocess.run(["kubectl", "--kubeconfig", str(KUBECONFIG), "--context", "kind-corca-lab",
                             *map(str, args)], input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=timeout, check=True)
    return result.stdout


def prometheus(query, start=None, end=None, step=5):
    parameters = {"query": query}
    endpoint = "query"
    if start is not None:
        endpoint = "query_range"
        parameters.update(start=start, end=end, step=step)
    path = "/api/v1/namespaces/corca/services/http:prometheus:9090/proxy/api/v1/" + endpoint
    result = json.loads(kube("get", "--raw", path + "?" + urllib.parse.urlencode(parameters)))
    if result.get("status") != "success":
        raise ValueError("Prometheus query failed")
    return result


def attributes(values):
    return {item["key"]: next(iter(item.get("value", {}).values()), None) for item in values}


def spans_from_records(records):
    for record in records:
        for resource in record.get("resourceSpans", []):
            service = attributes(resource.get("resource", {}).get("attributes", [])).get("service.name", "")
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    yield service, span


def target_of(span):
    attr = attributes(span.get("attributes", []))
    rpc = attr.get("rpc.service", str(span.get("name", "")).split("/")[0])
    return RPC_SERVICES.get(str(rpc).split(".")[-1])


def trace_features(records, grid, step=5, normal_end=None):
    samples = list(spans_from_records(records))
    normal_servers = {service for service, span in samples if span.get("kind") in (2, "SPAN_KIND_SERVER")
                      and int(span.get("startTimeUnixNano", 0)) / 1e9 < normal_end}
    buckets, edges = {}, set()
    for service, span in samples:
        timestamp = int(span.get("startTimeUnixNano", 0)) / 1e9
        end = int(span.get("endTimeUnixNano", 0)) / 1e9
        name = str(span.get("name", ""))
        if "health" in name.lower() or end < timestamp:
            continue
        target = target_of(span)
        client = span.get("kind") in (3, "SPAN_KIND_CLIENT")
        if client and target and service in SERVICES and timestamp < normal_end:
            edges.add((service, target))
        measured = target if client and target not in normal_servers else service
        if client and (not target or target in normal_servers):
            continue
        if measured not in SERVICES:
            continue
        bucket = int(timestamp // step) * step
        if bucket not in grid:
            continue
        error = span.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")
        buckets.setdefault((measured, bucket), []).append(((end - timestamp) * 1000, int(error)))
    columns = {}
    for service in {s for s, _ in buckets}:
        values = [buckets.get((service, t), []) for t in grid]
        columns[service + "_latency-90"] = [float(np.quantile([v[0] for v in bucket], .9)) if bucket else np.nan for bucket in values]
        columns[service + "_error"] = [sum(v[1] for v in bucket) / len(bucket) if bucket else np.nan for bucket in values]
        columns[service + "_workload"] = [len(bucket) for bucket in values]
    return pd.DataFrame(columns, index=grid), sorted(edges)


def trace_snapshot(start, end):
    names = kube("-n", "corca", "exec", "deployment/otel-collector", "-c", "trace-files", "--", "ls", "/data").splitlines()
    records = []
    for name in names:
        if not name.startswith("traces") or not name.endswith(".jsonl") or "/" in name:
            continue
        text = kube("-n", "corca", "exec", "deployment/otel-collector", "-c", "trace-files", "--", "cat", "/data/" + name)
        for index, line in enumerate(text.splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index == len(text.splitlines()) - 1:
                    continue  # The exporter may still be flushing its final line.
                raise
            resources = []
            for resource in record.get("resourceSpans", []):
                scopes = []
                for scope in resource.get("scopeSpans", []):
                    spans = [s for s in scope.get("spans", []) if start <= int(s.get("startTimeUnixNano", 0)) / 1e9 <= end]
                    if spans:
                        scopes.append(dict(scope, spans=spans))
                if scopes:
                    resources.append(dict(resource, scopeSpans=scopes))
            if resources:
                records.append({"resourceSpans": resources})
    return records


def collect(folder, start, first_injected, all_injected, end, step=5):
    folder = Path(folder)
    grid = list(range(int(start // step) * step + step, int(end // step) * step + 1, step))
    pods = json.loads(kube("-n", "corca", "get", "pods", "-o", "json"))
    mapping = {p["metadata"]["name"]: service_of(p["metadata"].get("labels", {}).get("app", "")) for p in pods["items"]}
    data = pd.DataFrame(index=grid)
    expressions = {
        "cpu": 'sum by(pod)(irate(container_cpu_usage_seconds_total{namespace="corca",container!="",container!="POD"}[60s]))',
        "mem": 'sum by(pod)(container_memory_working_set_bytes{namespace="corca",container!="",container!="POD"})',
    }
    for kind, query in expressions.items():
        response = prometheus(query, grid[0], grid[-1], step)
        (folder / f"raw-{kind}.json").write_text(json.dumps(response))
        for series in response["data"]["result"]:
            service = mapping.get(series["metric"].get("pod"))
            if service not in SERVICES:
                continue
            name = service + "_" + kind
            values = pd.Series({int(float(t)): float(v) for t, v in series["values"]}).reindex(grid)
            data[name] = data[name].add(values, fill_value=0) if name in data else values
    traces = trace_snapshot(start, end)
    (folder / "raw-traces.jsonl").write_text("".join(json.dumps(r) + "\n" for r in traces))
    features, edges = trace_features(traces, grid, step, first_injected)
    data = data.join(features)
    since = dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    logs = folder / "logs"
    logs.mkdir(exist_ok=True)
    for pod, service in mapping.items():
        if service not in SERVICES:
            continue
        text = kube("-n", "corca", "logs", pod, "--timestamps", "--since-time", since, "--limit-bytes=1048576")
        (logs / (pod + ".log")).write_text(text)
        errors = pd.Series(0.0, index=grid)
        for line in text.splitlines():
            prefix, _, message = line.partition(" ")
            try:
                timestamp = dt.datetime.fromisoformat(prefix.replace("Z", "+00:00")).timestamp()
                record = json.loads(message)
                level = record.get("severity", record.get("level", ""))
                is_error = level >= 50 if isinstance(level, int) else str(level).lower() in ("error", "fatal", "critical")
            except (ValueError, TypeError):
                continue
            bucket = int(timestamp // step) * step
            if is_error and bucket in errors.index:
                errors[bucket] += 1
        name = service + "_log-error"
        data[name] = data[name].add(errors, fill_value=0) if name in data else errors
    # Remove injection-transition and resource-rate warm-up samples.
    data = data[(data.index < first_injected - step) | (data.index >= all_injected + 30)]
    data.insert(0, "time", data.index)
    if min(sum(data["time"] < all_injected), sum(data["time"] >= all_injected)) < 10:
        raise ValueError("incident has insufficient normal/anomalous samples")
    if not edges:
        raise ValueError("no RPC topology was observed in the normal window")
    cpu_columns = [c for c in data if c.endswith("_cpu")]
    if len(cpu_columns) < len(SERVICES) or data[cpu_columns].isna().mean().max() > .1:
        raise ValueError("full-telemetry collection requires CPU coverage of at least 90% for every service")
    data.to_csv(folder / "metrics.csv", index=False)
    (folder / "inject_time.txt").write_text(str(int(all_injected)) + "\n")
    (folder / "topology.json").write_text(json.dumps({"edges": edges, "source": "normal_window_client_spans"}, indent=2))
    missing = data.drop(columns="time").isna().mean().to_dict()
    (folder / "telemetry-quality.json").write_text(json.dumps({"missing_fraction": missing,
                                                              "normal_samples": int(sum(data["time"] < all_injected)),
                                                              "anomalous_samples": int(sum(data["time"] >= all_injected))}, indent=2))
    return data
