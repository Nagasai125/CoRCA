"""Read-only provider adapters. Source files hold environment-variable names, never keys."""
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pandas as pd
import requests

from corca.topology import metric_column, raw_service_of
from runtime.prometheus import fetch_window as prometheus_window, validate_source as validate_prometheus
from runtime.credentials import credential_value

PROVIDERS = ("prometheus", "grafana", "datadog", "dynatrace", "newrelic")
PROMETHEUS = ("prometheus", "grafana")


def validate_source(config):
    if not isinstance(config, dict):
        raise ValueError("Source must be a JSON object")
    source = dict(config)
    provider = source.setdefault("provider", "prometheus")
    if provider not in PROVIDERS:
        raise ValueError("Choose Prometheus, Grafana, Datadog, Dynatrace, or New Relic")
    fields = {"provider", "prometheus_url", "url", "service_label", "step", "queries", "services", "edges",
              "alert_lead_seconds", "alert_method", "token_env", "username", "api_key_env", "app_key_env", "account_id"}
    if set(source) - fields:
        raise ValueError("Unknown source fields; credentials must be environment-variable references")
    url_key = "prometheus_url" if provider in PROMETHEUS else "url"
    if not isinstance(source.get(url_key), str):
        raise ValueError("A telemetry endpoint URL is required")
    url = urlsplit(source[url_key])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Use an HTTP(S) endpoint without embedded credentials, query parameters, or fragments")
    _ = url.port  # reject malformed ports before saving a connection
    if provider != "prometheus" and url.scheme != "https":
        raise ValueError("Vendor API credentials require an HTTPS endpoint")
    if provider == "datadog" and url.hostname not in {
            "api.datadoghq.com", "api.datadoghq.eu", "api.us3.datadoghq.com", "api.us5.datadoghq.com",
            "api.ap1.datadoghq.com", "api.ap2.datadoghq.com", "api.uk1.datadoghq.com", "api.ddog-gov.com"}:
        raise ValueError("Use the Datadog API endpoint for your region")
    if provider == "newrelic" and (url.hostname not in {"api.newrelic.com", "api.eu.newrelic.com", "api.jp.newrelic.com"}
                                  or url.path.rstrip("/") != "/graphql"):
        raise ValueError("Use a New Relic regional NerdGraph /graphql endpoint")
    for field in ("token_env", "api_key_env", "app_key_env"):
        if field in source and (not isinstance(source[field], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", source[field])):
            raise ValueError(f"{field} must name a server environment variable, not contain a secret")
    required = {"grafana": ("token_env", "username"), "datadog": ("api_key_env", "app_key_env"),
                "dynatrace": ("token_env",), "newrelic": ("token_env", "account_id")}.get(provider, ())
    if any(not source.get(field) for field in required):
        raise ValueError(f"{provider} requires {', '.join(required)}")
    if "username" in source and (not isinstance(source["username"], str) or not source["username"].strip()):
        raise ValueError("Basic-auth username must be a nonempty string")
    if "account_id" in source and (isinstance(source["account_id"], bool) or not isinstance(source["account_id"], int) or source["account_id"] < 1):
        raise ValueError("New Relic account ID must be a positive integer")
    source.setdefault("step", 5 if provider in PROMETHEUS else 60)
    source.setdefault("service_label", "dt.entity.service" if provider == "dynatrace" else "service")
    if not isinstance(source["service_label"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", source["service_label"]):
        raise ValueError("Service label must be a valid label, dimension, or attribute name")
    # Reuse the existing query/window/topology contract across providers.
    checked = validate_prometheus(dict(source, prometheus_url=source[url_key]))
    source.update({k: checked[k] for k in ("step", "alert_lead_seconds", "alert_method")})
    if len(source["queries"]) > 8 or any(len(q) > 12000 for q in source["queries"].values()):
        raise ValueError("Use at most eight metric queries, each at most 12,000 characters")
    if provider not in PROMETHEUS and source["step"] != 60:
        raise ValueError("Vendor adapters use 60-second buckets; set step to 60")
    names = source.get("services") or []
    if len(set(names)) != len(names):
        raise ValueError("Select each service only once")
    if len(names) * len(source["queries"]) > 199:
        raise ValueError("Select fewer services or metric queries; the beta diagnosis limit is 199 metric columns")
    if len(names) > 100 or len(source.get("edges") or []) > 1000:
        raise ValueError("Beta limit: 100 services and 1,000 dependencies")
    if any(len(n) > 64 or not n.strip() for n in names):
        raise ValueError("Service names must be 1–64 nonblank characters")
    if any(a == b or max(len(a), len(b)) > 64 for a, b in source.get("edges", [])):
        raise ValueError("Dependencies must connect two distinct service names")
    if not math.isfinite(source["alert_lead_seconds"]):
        raise ValueError("Alert lead must be finite")
    return source


def load_source(path):
    return validate_source(json.loads(Path(path).read_text()))


def secret(source, field):
    name = source.get(field)
    value = credential_value(source, name)
    if not value:
        raise ValueError(f"Set the {name or field} environment variable on the CoRCA server")
    return value


def request_json(method, url, **kwargs):
    response = requests.request(method, url, timeout=30, allow_redirects=False, **kwargs)
    response.raise_for_status()
    if 300 <= response.status_code < 400:
        raise ValueError("Provider redirects are disabled; configure the final API endpoint")
    return response.json()


def fetch_window(source, start, end):
    provider = source.get("provider", "prometheus")
    if provider in PROMETHEUS:
        return prometheus_window(source, start, end)
    step, label = source["step"], source["service_label"]
    grid = np.arange(math.ceil(start / step) * step, math.floor(end / step) * step + 1, step, dtype=np.int64)
    if len(grid) < 2:
        raise ValueError("Metric window is empty")
    data = {"time": grid}
    allowed = set(source.get("services") or [])

    def add(service, kind, points):
        if not service or (allowed and service not in allowed):
            return
        if not isinstance(service, str) or len(service) > 64:
            raise ValueError("Service names must be 1–64 characters")
        column = metric_column(service, kind)
        if column in data:
            raise ValueError(f"Multiple {kind} series for {service}; group by the service dimension only")
        values = pd.Series({int(t): float(v) if v is not None else np.nan for t, v in points}, dtype=float)
        data[column] = values.reindex(grid).to_numpy()  # never interpolate or manufacture observations

    for kind, query in source["queries"].items():
        if provider == "datadog":
            body = request_json("GET", source["url"].rstrip("/") + "/api/v1/query",
                                headers={"DD-API-KEY": secret(source, "api_key_env"), "DD-APPLICATION-KEY": secret(source, "app_key_env")},
                                params={"from": int(grid[0]), "to": int(grid[-1]), "query": query})
            if body.get("status") != "ok":
                raise ValueError("Datadog could not execute the metric query")
            for series in body.get("series", []):
                tags = series.get("tag_set") or series.get("scope", "").split(",")
                services = [tag.split(":", 1)[1] for tag in tags if tag.startswith(label + ":")]
                add(services[0] if len(services) == 1 else None, kind,
                    ((t / 1000, v) for t, v in series.get("pointlist", [])))
        elif provider == "dynatrace":
            body = request_json("GET", source["url"].rstrip("/") + "/api/v2/metrics/query",
                                headers={"Authorization": "Api-Token " + secret(source, "token_env")},
                                params={"metricSelector": query, "from": str(int(grid[0] * 1000)),
                                        "to": str(int(grid[-1] * 1000)), "resolution": "1m"})
            if body.get("nextPageKey"):
                raise ValueError("Dynatrace returned a partial page; narrow the metric selector")
            for result in body.get("result", []):
                for series in result.get("data", []):
                    add((series.get("dimensionMap") or {}).get(label), kind,
                        ((t / 1000, v) for t, v in zip(series["timestamps"], series["values"], strict=True)))
        elif provider == "newrelic":
            if not re.match(r"\s*SELECT\b", query, re.I) or re.search(r"\b(SINCE|UNTIL|TIMESERIES|COMPARE|SLIDE|LIMIT)\b|;", query, re.I):
                raise ValueError("NRQL must be a SELECT query without time, LIMIT, or comparison clauses; use AS value and one service FACET")
            nrql = f"{query} SINCE {int(grid[0])} UNTIL {int(grid[-1])} TIMESERIES 1 minute LIMIT MAX"
            graphql = "{ actor { account(id: " + str(source["account_id"]) + ") { nrql(query: " + json.dumps(nrql) + ") { results } } } }"
            body = request_json("POST", source["url"], headers={"API-Key": secret(source, "token_env")},
                                json={"query": graphql})
            if body.get("errors"):
                raise ValueError("New Relic rejected the query; check account access and NRQL")
            rows = body["data"]["actor"]["account"]["nrql"]["results"]
            groups = {}
            for row in rows:
                service = row.get("facet", row.get(label))
                if isinstance(service, list):
                    if len(service) != 1:
                        raise ValueError("Use exactly one service FACET in NRQL")
                    service = service[0]
                if "beginTimeSeconds" not in row or "value" not in row:
                    raise ValueError("NRQL must return time buckets, one service FACET, and a numeric AS value")
                groups.setdefault(service, []).append((row["beginTimeSeconds"], row["value"]))
            for service, points in groups.items():
                add(service, kind, points)
    frame = pd.DataFrame(data).replace([np.inf, -np.inf], np.nan)
    if frame.shape[1] < 2:
        raise ValueError("No service-labelled metric series returned; check the queries and service dimension")
    return frame


def discover_services(frame, source):
    """Summarize observed coverage without claiming that metric names reveal dependencies."""
    metrics = frame.drop(columns=["time"], errors="ignore").replace([np.inf, -np.inf], np.nan)
    names = set(source.get("services") or []) | {raw_service_of(c) for c in metrics.columns}
    services = []
    columns = {(raw_service_of(column), column.split("_", 1)[1]): column for column in metrics.columns}
    for name in sorted(names):
        coverage = {kind: {"samples": int(metrics[columns[name, kind]].notna().sum()),
                           "coverage": round(float(metrics[columns[name, kind]].notna().mean()), 3) if len(metrics) else 0}
                    for kind in source["queries"] if (name, kind) in columns}
        observed = [kind for kind, metric in coverage.items() if metric["samples"] > 0]
        services.append({"name": name, "metric_kinds": observed, "coverage": coverage,
                         "missing_kinds": sorted(set(source["queries"]) - set(observed)),
                         "status": "no_data" if not observed else "gaps" if any(m["coverage"] < .8 for m in coverage.values()) or len(observed) < len(source["queries"]) else "reporting"})
    return {"services": services, "sample_count": len(frame), "metric_kinds": sorted(source["queries"]),
            "dependencies": source.get("edges", []), "topology_configured": "edges" in source,
            "resource_only": not any(k.startswith("latency") or k == "error" for k in source["queries"])}
