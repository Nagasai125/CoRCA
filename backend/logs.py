"""Bounded, deterministic observations from uploaded logs; no causal inference."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
import re

MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_LOG_LINES = 50_000
ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?")
LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|ERR|FATAL|CRITICAL|PANIC)\b", re.I)
SERVICE = re.compile(r'''\b(?:service(?:\.name)?|app)\s*[=:]\s*["']?([\w.:/-]{1,128})''')
EXCEPTION = re.compile(r"(?:\bTraceback\b|\b\w*(?:Exception|Error):)")


def timestamp(value):
    """Return UTC seconds and whether a timezone was assumed."""
    try:
        if isinstance(value, bool) or value is None:
            return None, False
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{10}(?:\.\d+)?|\d{13}", str(value)):
            seconds = float(value)
            if seconds > 100_000_000_000:
                seconds /= 1000
            return (seconds, False) if math.isfinite(seconds) and 0 <= seconds <= 253402300799 else (None, False)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(",", "."))
        naive = dt.tzinfo is None
        return dt.replace(tzinfo=timezone.utc).timestamp() if naive else dt.timestamp(), naive
    except (ValueError, TypeError, OverflowError):
        return None, False


def signature(message):
    message = ISO_TIME.sub("", message)
    message = LEVEL.sub("", message, count=1)
    message = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b0x[0-9a-f]+\b", "<id>", message, flags=re.I)
    message = re.sub(r"\b\d+(?:\.\d+)*\b", "<n>", message)
    return re.sub(r"\s+", " ", message).strip(" []:-")[:240] or "Unspecified message"


def analyze_logs(content, filename, service="", incident_start=None):
    if len(content) > MAX_LOG_BYTES:
        raise ValueError("Choose a log file no larger than 2 MB.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Use a UTF-8 text or JSON-lines log file.") from None
    if "\x00" in text or not text.strip():
        raise ValueError("Choose a nonempty text log file, not a binary file.")
    lines = text.splitlines()
    if len(lines) > MAX_LOG_LINES or any(len(line) > 16384 for line in lines):
        raise ValueError("Use at most 50,000 lines, with each line no longer than 16,384 characters.")
    records, malformed, assumed = [], 0, 0
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        data = {}
        if line.lstrip().startswith("{"):
            try:
                data = json.loads(line)
            except (ValueError, RecursionError):
                malformed += 1
        if not isinstance(data, dict):
            data = {}
        match = ISO_TIME.search(line)
        stamp, naive = timestamp(next((data[k] for k in ("timestamp", "time", "@timestamp", "ts") if k in data), match.group() if match and not data else None))
        level_value = data.get("level", data.get("severity", data.get("severity_text", data.get("severityText", ""))))
        level_match = LEVEL.search(str(level_value)) or LEVEL.search(line)
        level = level_match.group().upper() if level_match else "UNKNOWN"
        level = "ERROR" if level in ("ERROR", "ERR", "FATAL", "CRITICAL", "PANIC") else "WARN" if level in ("WARN", "WARNING") else level
        continuation = not data and stamp is None and not level_match and records and (line[:1].isspace() or line.startswith(("Traceback", "Caused by:", "During handling")) or EXCEPTION.search(line))
        if continuation:
            record = records[-1]
            record["line_end"] = number
            record["sample"] = (record["sample"] + "\n" + line)[:1200]
            if EXCEPTION.search(line):
                record["level"] = "ERROR"
                if not line.startswith("Traceback"):
                    record["message"] = line.strip()[:2000]
            continue
        if level == "UNKNOWN" and EXCEPTION.search(line):
            level = "ERROR"
        resource = data.get("resource", {})
        resource = resource if isinstance(resource, dict) else {}
        attributes = resource.get("attributes", {})
        attributes = attributes if isinstance(attributes, dict) else {}
        service_value = data.get("service.name", data.get("service", resource.get("service.name", attributes.get("service.name", ""))))
        if isinstance(service_value, dict):
            service_value = service_value.get("name", "")
        service_match = SERVICE.search(line) if not service_value else None
        name = service_value or (service_match.group(1) if service_match else service.strip()) or "Unknown service"
        if not isinstance(name, str):
            name = service.strip() or "Unknown service"
        message = data.get("message", data.get("msg", data.get("body", line)))
        message = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        records.append({"line": number, "line_end": number, "timestamp": stamp, "service": name[:128],
                        "level": level, "message": message[:2000], "sample": line[:1200]})
        assumed += naive
    if not records:
        raise ValueError("No log entries were found.")
    services = defaultdict(Counter)
    groups = {}
    times = [r["timestamp"] for r in records if r["timestamp"] is not None]
    errors = [r for r in records if r["level"] == "ERROR"]
    for r in records:
        services[r["service"]]["entries"] += 1
        services[r["service"]]["errors"] += r["level"] == "ERROR"
        services[r["service"]]["warnings"] += r["level"] == "WARN"
        if r["level"] not in ("ERROR", "WARN"):
            continue
        key = (r["level"], signature(r["message"]))
        if key not in groups:
            groups[key] = {"level": key[0], "pattern": key[1], "count": 0, "services": set(), "samples": []}
        group = groups[key]
        group["count"] += 1
        group["services"].add(r["service"])
        if len(group["samples"]) < 3:
            group["samples"].append({k: r[k] for k in ("line", "line_end", "timestamp", "service", "sample")})
    patterns = sorted(groups.values(), key=lambda g: (-g["count"], g["level"], g["pattern"]))[:10]
    for group in patterns:
        group["services"] = sorted(group["services"])[:20]
    buckets = defaultdict(Counter)
    interval = max(60, math.ceil((max(times) - min(times) + 1) / 20 / 60) * 60) if times else 60
    for r in records:
        if r["timestamp"] is not None:
            bucket = buckets[math.floor(r["timestamp"] / interval) * interval]
            bucket["entries"] += 1
            bucket["errors"] += r["level"] == "ERROR"
            bucket["warnings"] += r["level"] == "WARN"
    notes = []
    if len(times) < len(records):
        notes.append(f"{len(records) - len(times)} entries have no recognized timestamp and are excluded from the timeline and incident comparison.")
    if assumed:
        notes.append(f"{assumed} timestamps have no timezone; UTC was assumed.")
    if malformed:
        notes.append(f"{malformed} lines beginning with '{{' were not valid JSON and were read as plain text.")
    unknown = sum(r["level"] == "UNKNOWN" for r in records)
    if unknown:
        notes.append(f"{unknown} entries have no recognized severity; they are not counted as errors or warnings.")
    if len(groups) > 10:
        notes.append(f"Showing the 10 most frequent of {len(groups)} error/warning patterns.")
    if len(services) > 20:
        notes.append(f"Showing the 20 most active of {len(services)} services.")
    comparison = None
    if incident_start is not None:
        before = [r for r in records if r["timestamp"] is not None and r["timestamp"] < incident_start]
        after = [r for r in records if r["timestamp"] is not None and r["timestamp"] >= incident_start]
        comparison = {"incident_start": incident_start, "before": {"entries": len(before), "errors": sum(r["level"] == "ERROR" for r in before)},
                      "after": {"entries": len(after), "errors": sum(r["level"] == "ERROR" for r in after)}}
        if not before or not after:
            notes.append("Timed entries do not cover both sides of the selected incident start.")
    return {"filename": filename, "kind": "log_observations", "entries": len(records), "lines": len(lines),
            "errors": len(errors), "warnings": sum(r["level"] == "WARN" for r in records), "timestamped": len(times),
            "range": [min(times), max(times)] if times else None, "notes": notes,
            "services": [{"name": name, **counts} for name, counts in sorted(services.items(), key=lambda item: (-item[1]["entries"], item[0]))[:20]],
            "patterns": patterns, "timeline_interval_seconds": interval,
            "timeline": [{"start": start, **counts} for start, counts in sorted(buckets.items())], "comparison": comparison}
