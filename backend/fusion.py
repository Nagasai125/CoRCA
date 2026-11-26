"""Align OTLP JSON logs and traces with an incident's metric clock."""
from collections import defaultdict
import math
import re

import numpy as np
import pandas as pd

from corca.topology import metric_column, service_of

FUSION_POLICY = "otlp-aligned-features-v1"
MAX_EVENTS = 20000


def _exports(value):
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if not all(isinstance(item, dict) for item in values):
        raise ValueError("Telemetry must contain OTLP JSON export objects")
    return values


def _attributes(resource):
    attributes = resource.get("attributes", [])
    if not isinstance(attributes, list):
        raise ValueError("OTLP resource attributes must be a list")
    return {a.get("key"): a.get("value", {}).get("stringValue")
            for a in attributes if isinstance(a, dict) and isinstance(a.get("value", {}), dict)}


def _events(value, resources, scopes, records):
    count = 0
    for export in _exports(value):
        groups = export.get(resources)
        if not isinstance(groups, list):
            raise ValueError(f"Expected OTLP {resources}")
        for group in groups:
            service = _attributes(group.get("resource", {})).get("service.name")
            if not isinstance(service, str) or not service.strip() or len(service) > 64:
                raise ValueError("Every telemetry resource needs service.name (1–64 characters)")
            for scope in group.get(scopes, []):
                for event in scope.get(records, []):
                    count += 1
                    if count > MAX_EVENTS:
                        raise ValueError(f"Use at most {MAX_EVENTS} {records} per investigation")
                    if not isinstance(event, dict):
                        raise ValueError(f"Invalid OTLP {records} record")
                    yield service, event


def _seconds(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,21}", str(value)):
        raise ValueError("OTLP timestamps must be Unix nanoseconds")
    result = int(value) / 1e9
    if not 0 < result <= 253402300799:
        raise ValueError("OTLP timestamp is outside the supported range")
    return result


def _identifier(value, width, optional=False):
    if optional and (value in (None, "") or value == "0" * width):
        return ""
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-fA-F]{{{width}}}", value) or int(value, 16) == 0:
        raise ValueError("OTLP trace/span IDs must be nonzero hexadecimal identifiers")
    return value.lower()


def fuse(frame, onset, traces=None, logs=None):
    """Missing buckets stay missing; only observed parent/child spans create edges."""
    times = frame["time"].to_numpy(float)
    step = float(np.median(np.diff(times)))
    if not math.isfinite(step) or step <= 0:
        raise ValueError("A valid increasing metric clock is required")
    output = frame.copy()
    modalities = {c: "metrics" for c in frame if c != "time"}
    notes, spans = [], {}
    for service, span in _events(traces, "resourceSpans", "scopeSpans", "spans"):
        start, end = _seconds(span.get("startTimeUnixNano")), _seconds(span.get("endTimeUnixNano"))
        if end < start:
            raise ValueError("A span cannot end before it starts")
        key = (_identifier(span.get("traceId"), 32), _identifier(span.get("spanId"), 16))
        parent = _identifier(span.get("parentSpanId"), 16, True)
        item = {"service": service, "start": start, "end": end, "parent": parent,
                "kind": span.get("kind", 0), "error": span.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")}
        if key in spans and spans[key] != item:
            raise ValueError("Conflicting duplicate span IDs")
        spans[key] = item

    def bucket(stamp):
        index = int(np.searchsorted(times, stamp, side="right") - 1)
        return index if 0 <= index < len(times) and stamp < times[index] + step else None

    trace_buckets, log_buckets = defaultdict(list), defaultdict(list)
    before, after = set(), set()
    orphaned, linked, ignored = 0, 0, 0
    server_services = {s["service"] for s in spans.values() if s["kind"] in (2, 5, "SPAN_KIND_SERVER", "SPAN_KIND_CONSUMER") and bucket(s["start"]) is not None}
    for (trace, _), span in spans.items():
        index = bucket(span["start"])
        if index is None:
            ignored += 1
            continue
        parent = spans.get((trace, span["parent"]))
        if span["parent"] and parent is None:
            orphaned += 1
        if parent and parent["service"] != span["service"]:
            edge = (parent["service"], span["service"])
            (before if span["start"] < onset else after).add(edge)
        if span["service"] not in server_services or span["kind"] in (2, 5, "SPAN_KIND_SERVER", "SPAN_KIND_CONSUMER"):
            trace_buckets[span["service"], index].append(((span["end"] - span["start"]) * 1000, span["error"]))
    seen_logs = set()
    for service, record in _events(logs, "resourceLogs", "scopeLogs", "logRecords"):
        stamp = _seconds(record.get("timeUnixNano") or record.get("observedTimeUnixNano"))
        severity = record.get("severityNumber")
        if isinstance(severity, bool) or not isinstance(severity, int) or not 1 <= severity <= 24:
            raise ValueError("Log records need OTLP severityNumber (1–24)")
        trace = _identifier(record.get("traceId"), 32, True)
        span = _identifier(record.get("spanId"), 16, True)
        if span and not trace:
            raise ValueError("A log spanId requires its traceId")
        # Repeated export records must not inflate evidence.
        identity = (service, stamp, severity, trace, span, str(record.get("body", "")))
        if identity in seen_logs:
            continue
        seen_logs.add(identity)
        index = bucket(stamp)
        if index is None:
            ignored += 1
            continue
        log_buckets[service, index].append(severity >= 17)
        linked += bool(trace and span and (trace, span) in spans and spans[trace, span]["service"] == service)

    def add(service, kind, values, modality):
        column = metric_column(service, kind)
        if column in output:
            raise ValueError(f"Uploaded metrics already contain derived column {column}")
        output[column] = values
        modalities[column] = modality

    for service in sorted({s for s, _ in trace_buckets}):
        values = [trace_buckets.get((service, i), []) for i in range(len(times))]
        add(service, "trace-latency-90", [float(np.quantile([v[0] for v in x], .9)) if x else np.nan for x in values], "traces")
        add(service, "trace-error", [sum(v[1] for v in x) / len(x) if x else np.nan for x in values], "traces")
    for service in sorted({s for s, _ in log_buckets}):
        add(service, "log-error", [float(sum(log_buckets[service, i])) if (service, i) in log_buckets else np.nan for i in range(len(times))], "logs")
    if len(output.columns) > 200 or len({service_of(c) for c in output if c != "time"}) > 100:
        raise ValueError("Combined evidence exceeds 199 metric columns or 100 services")
    if len(before | after) > 1000:
        raise ValueError("Trace topology exceeds 1,000 dependencies")
    if traces is not None and not trace_buckets:
        raise ValueError("No usable spans overlap the metric window")
    if logs is not None and not log_buckets:
        raise ValueError("No usable log records overlap the metric window")
    if orphaned:
        notes.append(f"{orphaned} spans have missing parents; their unobserved dependencies were not inferred.")
    if ignored:
        notes.append(f"{ignored} records fall outside observed metric buckets.")
    notes.append("Empty log/trace buckets remain missing; exported traffic may be sampled.")
    return output, {"policy": FUSION_POLICY, "modalities": modalities, "span_count": len(spans),
                    "log_count": len(seen_logs), "linked_log_count": linked, "notes": notes,
                    "topology": {"source": "trace_parent_child", "edges": sorted(before | after),
                                 "normal_edges": sorted(before), "incident_edges": sorted(after),
                                 "added": sorted(after - before), "not_observed_during_incident": sorted(before - after)}}

