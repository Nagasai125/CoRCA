"""Persistent last-reported Alertmanager events, linked to diagnosis jobs."""
import hashlib
import json
import time


class AlertInbox:
    def __init__(self, jobs):
        self.jobs = jobs
        with jobs._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS alert_events (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, service TEXT NOT NULL, severity TEXT NOT NULL,
                summary TEXT NOT NULL, status TEXT NOT NULL, starts_at REAL NOT NULL, ends_at REAL,
                first_received REAL NOT NULL, last_received REAL NOT NULL, deliveries INTEGER NOT NULL,
                origin TEXT NOT NULL, job_id TEXT, diagnosis_error TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS alert_events_received ON alert_events(last_received DESC)")

    def record(self, hook, origin="alertmanager"):
        now, events = time.time(), []
        with self.jobs._db() as db:
            for alert in hook.alerts:
                identity = alert.fingerprint or (json.dumps(alert.labels, sort_keys=True) if alert.labels else hook.groupKey)
                event_id = hashlib.sha256(f"{origin}:{identity}:{alert.startsAt.timestamp()}".encode()).hexdigest()[:32]
                labels = alert.labels
                state = "resolved" if hook.status == "resolved" else alert.status
                db.execute("""INSERT INTO alert_events
                    (id,name,service,severity,summary,status,starts_at,ends_at,first_received,last_received,deliveries,origin)
                    VALUES (?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,service=excluded.service,severity=excluded.severity,summary=excluded.summary,
                    status=CASE WHEN alert_events.status='resolved' THEN 'resolved' ELSE excluded.status END,
                    ends_at=CASE WHEN alert_events.status='resolved' AND excluded.status='firing' THEN alert_events.ends_at ELSE COALESCE(excluded.ends_at,alert_events.ends_at) END,
                    diagnosis_error=CASE WHEN excluded.status='resolved' THEN NULL ELSE alert_events.diagnosis_error END,
                    last_received=excluded.last_received,
                    deliveries=alert_events.deliveries+1""",
                    (event_id, labels.get("alertname", "Unnamed alert")[:256],
                     (labels.get("service") or labels.get("service_name") or labels.get("job") or labels.get("instance") or "Unknown")[:256],
                     labels.get("severity", "unknown")[:64], alert.annotations.get("summary", "")[:1000], state,
                     alert.startsAt.timestamp(), alert.endsAt.timestamp() if alert.endsAt else None, now, now, origin))
                effective = db.execute("SELECT status FROM alert_events WHERE id=?", (event_id,)).fetchone()[0]
                events.append({"id": event_id, "firing": state == "firing" and effective == "firing", "starts_at": alert.startsAt.timestamp()})
        return events

    def link(self, identifiers, job_id=None, error=None):
        with self.jobs._db() as db:
            db.executemany("UPDATE alert_events SET job_id=COALESCE(?,job_id),diagnosis_error=? WHERE id=?",
                           [(job_id, error, identifier) for identifier in identifiers])

    def metadata(self):
        with self.jobs._db() as db:
            row = db.execute("""SELECT MAX(last_received) AS last_received_at,
                MAX(CASE WHEN origin='alertmanager' THEN last_received END) AS last_external_at,
                SUM(CASE WHEN status='firing' AND origin='alertmanager' THEN 1 ELSE 0 END) AS firing,
                SUM(CASE WHEN status='resolved' AND origin='alertmanager' THEN 1 ELSE 0 END) AS resolved
                FROM alert_events""").fetchone()
        return {key: row[key] if row[key] is not None else (0 if key in ("firing", "resolved") else None) for key in row.keys()}

    def recent(self, status="all", query="", limit=200):
        conditions, arguments = [], []
        if status != "all":
            conditions.append("e.status=?"); arguments.append(status)
        if query:
            conditions.append("instr(lower(e.name||' '||e.service||' '||e.summary),?)>0"); arguments.append(query.lower())
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.jobs._db() as db:
            total = db.execute("SELECT count(*) FROM alert_events e" + where, arguments).fetchone()[0]
            rows = db.execute("""SELECT e.*,j.status AS diagnosis_status FROM alert_events e
                LEFT JOIN jobs j ON j.id=e.job_id""" + where + " ORDER BY e.last_received DESC,e.id LIMIT ?", [*arguments, limit]).fetchall()
        return {"alerts": [dict(row) for row in rows], "matching": total, "delivery": self.metadata()}
