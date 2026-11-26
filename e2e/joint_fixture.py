"""Deterministic synthetic OTLP/metric windows for software acceptance only."""
import math


def fixture(healthy=False):
    rows, logs, spans = [], [], {"frontend": [], "database": []}
    for i in range(240):
        stamp = 1700000000 + (3600 if healthy else 0) + 5 * i
        database = 10 + math.sin(i * .71) + (5 if i >= 200 and not healthy else 0)
        frontend = 2 * database + .03 * math.cos(i) + (3 if i >= 200 and not healthy else 0)
        rows.append([stamp, database, frontend])
        trace, parent, child = f"{i+1:032x}", f"{2*i+1:016x}", f"{2*i+2:016x}"
        for service, identifier, parent_id, duration in [("frontend", parent, "", frontend), ("database", child, parent, database)]:
            spans[service].append({"traceId":trace, "spanId":identifier, "parentSpanId":parent_id,
                "kind":2, "startTimeUnixNano":str(stamp*10**9),
                "endTimeUnixNano":str(stamp*10**9+int(duration*10**6)), "status":{"code":1}})
        logs.append({"timeUnixNano":str(stamp*10**9), "severityNumber":17 if i>=200 and not healthy else 9,
                     "traceId":trace, "spanId":child, "body":{"stringValue":"SYNTHETIC PRIVATE BODY"}})
    resource = lambda name: {"attributes":[{"key":"service.name", "value":{"stringValue":name}}]}
    return {"metrics":{"columns":["time", "~database_cpu", "~frontend_cpu"], "rows":rows, "inject_time":1700001000+(3600 if healthy else 0)},
            "traces":{"resourceSpans":[{"resource":resource(s), "scopeSpans":[{"spans":v}]} for s,v in spans.items()]},
            "logs":{"resourceLogs":[{"resource":resource("database"), "scopeLogs":[{"logRecords":logs}]}]}}

