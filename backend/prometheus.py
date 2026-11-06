"""Pull a diagnosis window straight from Prometheus, so callers never build metric matrices.

A source config (JSON) says where Prometheus is and which PromQL gives each metric kind.
Every query must return one series per service, labelled with `service_label`.
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from corca.topology import metric_column
from backend.credentials import credential_value


def validate_source(source):
    if not str(source.get("prometheus_url", "")).startswith(("http://", "https://")):
        raise ValueError("source.prometheus_url must be an http(s) URL")
    queries = source.get("queries")
    if not isinstance(queries, dict) or not queries or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in queries.items()):
        raise ValueError("source.queries must map metric kinds (cpu, mem, latency-90, ...) to PromQL")
    source.setdefault("step", 5)
    source.setdefault("service_label", "service")
    source.setdefault("alert_lead_seconds", 60)
    source.setdefault("alert_method", "baro")
    step, lead = source["step"], source["alert_lead_seconds"]
    if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= 60:
        raise ValueError("source.step must be an integer of 1-60 seconds")
    if isinstance(lead, bool) or not isinstance(lead, (int, float)) or not 0 <= lead <= 3600:
        raise ValueError("source.alert_lead_seconds must be 0-3600")
    if source["alert_method"] not in ("baro", "circa"):
        raise ValueError("source.alert_method must be baro or circa")
    services, edges = source.get("services"), source.get("edges")
    if services is not None and (not isinstance(services, list) or not all(isinstance(s, str) and s for s in services)):
        raise ValueError("source.services must be a list of service names")
    if edges is not None and (not isinstance(edges, list) or not all(
            isinstance(e, list) and len(e) == 2 and all(isinstance(s, str) and s for s in e) for e in edges)):
        raise ValueError("source.edges must be a list of [caller, callee] pairs")
    return source


def load_source(path):
    return validate_source(json.loads(Path(path).read_text()))


def fetch_window(source, start, end):
    """Return a frame with `time` plus one `<service>_<kind>` column per series; gaps stay NaN."""
    step, label = source["step"], source["service_label"]
    allowed = set(source.get("services") or [])
    grid = np.arange(math.ceil(start / step) * step, math.floor(end / step) * step + 1, step, dtype=np.int64)
    if len(grid) < 2:
        raise ValueError("diagnosis window is empty")
    data = {"time": grid}
    auth = {}
    if source.get("token_env"):
        token = credential_value(source, source["token_env"])
        if not token:
            raise ValueError(f"Set {source['token_env']} on the CoRCA server")
        auth = ({"auth": (source["username"], token)} if source.get("username")
                else {"headers": {"Authorization": "Bearer " + token}})
    for kind, query in source["queries"].items():
        response = requests.get(source["prometheus_url"].rstrip("/") + "/api/v1/query_range", timeout=30,
                                params={"query": query, "start": int(grid[0]), "end": int(grid[-1]), "step": step},
                                allow_redirects=False, **auth)
        response.raise_for_status()
        if 300 <= getattr(response, "status_code", 200) < 400:
            raise ValueError("Configure the final Prometheus endpoint; redirects are disabled")
        body = response.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus query for {kind!r} failed: {body.get('error')}")
        for series in body["data"]["result"]:
            service = series["metric"].get(label)
            if not service or (allowed and service not in allowed):
                continue
            column = metric_column(service, kind, legacy_aliases="edges" not in source)
            if column in data:
                raise ValueError(f"{kind!r} query returned several series for {service}; aggregate by {label}")
            values = pd.Series({int(float(t)): float(v) for t, v in series["values"]}, dtype=float)
            data[column] = values.reindex(grid).to_numpy()
    frame = pd.DataFrame(data).replace([np.inf, -np.inf], np.nan)
    if frame.shape[1] < 2:
        raise RuntimeError("Prometheus returned no series for the configured services")
    return frame
