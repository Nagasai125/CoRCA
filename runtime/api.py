"""Bounded, authenticated incident diagnosis over metric windows."""
import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import networkx as nx
import numpy as np
import pandas as pd
import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from corca import corca
from corca.base import anomaly_scores
from corca.circa import FEATURE_POLICY, InsufficientEvidence
from corca.corca import STOPPING_POLICY
from corca.topology import service_of, raw_service_of
from runtime.telemetry import fetch_window, load_source, validate_source, discover_services


class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    columns: list[str] = Field(min_length=2, max_length=200)
    rows: list[list[float | None]] = Field(min_length=20, max_length=2500)
    inject_time: float
    method: Literal["baro", "circa"] = "baro"
    edges: list[tuple[str, str]] | None = Field(default=None, max_length=1000)
    tau: float | None = Field(default=None, ge=0, le=1e9)
    anomaly_threshold: float | None = Field(default=None, ge=0, le=1e9)

    @model_validator(mode="after")
    def valid_window(self):
        if len(set(self.columns)) != len(self.columns) or "time" not in self.columns:
            raise ValueError("columns must be unique and include time")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("row widths must match columns")
        timestamps = [r[self.columns.index("time")] for r in self.rows]
        if any(t is None for t in timestamps) or any(a >= b for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError("timestamps must be present and strictly increasing")
        if min(sum(t < self.inject_time for t in timestamps), sum(t >= self.inject_time for t in timestamps)) < 10:
            raise ValueError("at least 10 samples before and after inject_time are required")
        if any(len(c) > 128 or (c != "time" and "_" not in c) for c in self.columns):
            raise ValueError("metric columns must use service_metric naming")
        if self.edges is not None and any(not a or not b or len(a) > 64 or len(b) > 64 for a, b in self.edges):
            raise ValueError("graph services must be nonempty and at most 64 characters")
        return self


class WindowRequest(BaseModel):
    """Diagnose an incident by time only; the service pulls the metrics from Prometheus."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    inject_time: float = Field(gt=0, description="Unix seconds when the fault (or alert) started")
    normal_seconds: int = Field(default=300, ge=60, le=3600)
    anomaly_seconds: int = Field(default=120, ge=60, le=1800)
    method: Literal["baro", "circa"] = "baro"


class ConnectionSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: dict
    revision: str | None = None


class TopologySetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    services: list[str] = Field(min_length=1, max_length=100)
    edges: list[tuple[str, str]] = Field(max_length=1000)


class CalibrationSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    calibration: dict


class AlertmanagerAlert(BaseModel):
    status: str
    startsAt: datetime


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook payload (version 4); unknown fields are ignored."""
    status: str
    groupKey: str = Field(max_length=4096)
    alerts: list[AlertmanagerAlert] = Field(max_length=1000)


SCRAPE_LAG = 15        # seconds to wait after a window ends so Prometheus has the last samples
MAX_ATTEMPTS = 3       # transient Prometheus failures are retried; anything else fails at once
RETRY_SECONDS = 30


class Jobs:
    """Diagnosis jobs in SQLite: survive restarts, deduplicate alerts, keep history.

    shortcut: one writer process (replicas: 1, ReadWriteOnce volume); move to Postgres before scaling out.
    """

    def __init__(self, path):
        self.path = str(path)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, dedupe TEXT UNIQUE, method TEXT NOT NULL, inject_time REAL NOT NULL,
                start REAL NOT NULL, end REAL NOT NULL, ready_at REAL NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, result TEXT, error TEXT)""")
            if "context" not in {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN context TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")  # crashed mid-run

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def connection(self):
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE name = 'connection'").fetchone()
        return json.loads(row["value"]) if row else None

    def bind_legacy_jobs(self, context):
        with self._db() as db:
            db.execute("UPDATE jobs SET context=? WHERE context IS NULL AND status IN ('queued','running')",
                       (json.dumps(context),))

    def save_connection(self, context):
        context = {**context, "stopping_policy": STOPPING_POLICY, "feature_policy": FEATURE_POLICY}
        with self._db() as db:
            db.execute("INSERT INTO settings(name,value) VALUES ('connection',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       (json.dumps(context),))

    def add(self, method, inject_time, normal_seconds, anomaly_seconds, dedupe=None, context=None):
        end = inject_time + anomaly_seconds
        job_id = uuid.uuid4().hex
        with self._db() as db:
            try:
                db.execute("INSERT INTO jobs (id, dedupe, method, inject_time, start, end, ready_at, status, created, context)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                           (job_id, dedupe, method, inject_time, inject_time - normal_seconds, end,
                            end + SCRAPE_LAG, time.time(), json.dumps(context) if context else None))
            except sqlite3.IntegrityError:  # Alertmanager re-sent the same notification
                return self.view(db.execute("SELECT * FROM jobs WHERE dedupe = ?", (dedupe,)).fetchone()), False
        return self.get(job_id), True

    def claim(self):
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE status = 'queued' AND ready_at <= ? ORDER BY ready_at LIMIT 1",
                             (time.time(),)).fetchone()
            if row is not None:
                db.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1 WHERE id = ?", (row["id"],))
                return {**dict(row), "attempts": row["attempts"] + 1}
        return None

    def finish(self, job_id, status, result=None, error=None, ready_at=None):
        with self._db() as db:
            db.execute("UPDATE jobs SET status = ?, result = ?, error = ?, ready_at = COALESCE(?, ready_at) WHERE id = ?",
                       (status, None if result is None else json.dumps(result), error, ready_at, job_id))

    def get(self, job_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else self.view(row)

    def recent(self, limit):
        with self._db() as db:
            return [self.view(r) for r in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]

    @staticmethod
    def view(row):
        row = dict(row)
        return {"id": row["id"], "status": row["status"], "method": row["method"],
                "inject_time": row["inject_time"], "window": [row["start"], row["end"]],
                "created_at": row["created"], "attempts": row["attempts"], "error": row["error"],
                "result": None if row["result"] is None else json.loads(row["result"])}


def run_job(job, jobs, source, thresholds, timeout, fetch):
    try:
        context = json.loads(job.get("context") or "{}")
        if ("stopping_policy" in context and context["stopping_policy"] != STOPPING_POLICY or
                job["method"] == "circa" and "feature_policy" in context and context["feature_policy"] != FEATURE_POLICY):
            raise ValueError("Queued calibration policy is outdated; recalibrate before submitting a new diagnosis")
        source, thresholds = context.get("source", source), context.get("thresholds", thresholds)
        frame = fetch(source, job["start"], job["end"])
        rows = frame.astype(object).where(frame.notna(), None).values.tolist()
        incident = Incident(columns=list(frame.columns), rows=rows, inject_time=job["inject_time"],
                            method=job["method"], edges=source.get("edges"))
        jobs.finish(job["id"], "done", result=diagnose(incident, thresholds, timeout))
    except requests.RequestException as exc:
        logging.warning(json.dumps({"event": "job_retry", "id": job["id"], "error": str(exc)}))
        retry = job["attempts"] < MAX_ATTEMPTS
        jobs.finish(job["id"], "queued" if retry else "failed", error="Prometheus unavailable" if source.get("provider", "prometheus") == "prometheus" else "Telemetry provider unavailable",
                    ready_at=time.time() + RETRY_SECONDS)
    except Exception as exc:
        logging.exception("job %s failed", job["id"])
        jobs.finish(job["id"], "failed", error="The telemetry query or analysis failed. Check service logs and the source configuration.")


def work(jobs, source, thresholds, timeout, slots, fetch, stop):
    while not stop.wait(1):
        if not slots.acquire(blocking=False):
            continue
        try:
            job = jobs.claim()
            if job is not None:
                run_job(job, jobs, source, thresholds, timeout, fetch)
        except Exception:
            logging.exception("job worker error")  # keep the worker alive (e.g. database briefly locked)
        finally:
            slots.release()


class BodyLimit:
    def __init__(self, app, limit=4 * 1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        deadline = time.monotonic() + 10
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=max(0, deadline - time.monotonic()))
            except TimeoutError:
                await send({"type": "http.response.start", "status": 408,
                            "headers": [(b"content-type", b"application/json"), (b"connection", b"close")]})
                await send({"type": "http.response.body", "body": b'{"detail":"request body timeout"}'})
                return
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"request too large"}'})
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def prepare_window(incident):
    df = pd.DataFrame(incident.rows, columns=incident.columns)
    normal = df["time"] < incident.inject_time
    missing = df.drop(columns="time").isna().mean()
    unusable = [c for c in missing.index if missing[c] > 0.2
                or df.loc[normal, c].notna().sum() < 10 or df.loc[~normal, c].notna().sum() < 10]
    df = df.drop(columns=unusable)
    remaining = [c for c in df.columns if c != "time"]
    # Short gaps are filled locally; remaining gaps use only the normal-window median.
    df[remaining] = df[remaining].ffill(limit=2).fillna(df.loc[normal, remaining].median())
    return df, unusable, missing


def diagnose(incident, thresholds, timeout):
    tau = incident.tau if incident.tau is not None else thresholds.get(incident.method)
    if tau is None:
        raise HTTPException(422, "a calibrated tau or a configured calibration file is required")
    df, unusable, missing = prepare_window(incident)
    remaining = [c for c in df.columns if c != "time"]
    if not remaining:
        return {"status": "insufficient_evidence", "root_causes": [], "rounds": [], "dropped_metrics": unusable}
    scores = anomaly_scores(df, incident.inject_time)
    graph = None
    if incident.edges is not None:
        identities = {raw_service_of(column): service_of(column) for column in remaining}
        graph = nx.DiGraph((identities.get(a, a), identities.get(b, b)) for a, b in incident.edges)
        graph.add_nodes_from(service_of(c) for c in remaining)
    if incident.anomaly_threshold is not None and (scores.empty or scores.max() < incident.anomaly_threshold):
        return {"status": "no_significant_anomaly", "root_causes": [], "rounds": [],
                "dropped_metrics": unusable, "anomaly_threshold": incident.anomaly_threshold}
    try:
        result = corca(df, incident.inject_time, method=incident.method, graph=graph,
                       max_rounds=3, tau=tau, round_timeout=timeout, dataset="re2-ob")
    except InsufficientEvidence as exc:
        return {"status": "insufficient_evidence", "root_causes": [], "rounds": [],
                "reason": str(exc), "dropped_metrics": unusable}
    output = asdict(result)
    observed = {service_of(c) for c in remaining}
    for round_ in output["rounds"]:  # list only services we saw, not topology-only nodes like frontend-external
        round_["explained"] = [s for s in round_["explained"] if s in observed]
    output.update(status="partial_timeout" if result.timed_out else
                  "insufficient_evidence" if not result.root_causes else
                  "partial_unexplained" if result.remaining_anomaly_score > tau else "diagnosed",
                  unexplained_services=[s for s, v in result.remaining_service_scores.items() if v > tau],
                  tau=tau, dropped_metrics=unusable,
                  missing_fraction={c: float(v) for c, v in missing.items()},
                  graph_source="provided" if graph is not None else "static_online_boutique",
                  unobserved_services=sorted(set(graph or ()) - {service_of(c) for c in remaining}),
                  confidence_kind="anomaly_score_ratio_not_probability")
    return output


def calibration_thresholds(calibration):
    thresholds = {m: v["tau"] for m, v in calibration["methods"].items()}
    if any(m not in ("baro", "circa") or isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for m, v in thresholds.items()):
        raise ValueError("calibration thresholds must be finite, nonnegative numbers for supported methods")
    if "circa" in thresholds and calibration.get("feature_policies", {}).get("circa") != FEATURE_POLICY:
        raise ValueError("CIRCA calibration feature policy changed or is missing; recalibrate with current code")
    if thresholds and calibration.get("stopping_policy") != STOPPING_POLICY:
        raise ValueError("calibration stopping policy changed or is missing; recalibrate with current code")
    return thresholds


def create_app(token, thresholds=None, timeout=20, concurrency=2, source=None, db_path=None, fetch=fetch_window):
    if not token or len(token) < 16:
        raise ValueError("API token must be at least 16 characters")
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    jobs = Jobs(db_path) if db_path else None
    saved = jobs.connection() if jobs else None
    if saved:
        source = validate_source(saved["source"])
        thresholds = saved["thresholds"]
        if (saved.get("stopping_policy") != STOPPING_POLICY or
                "circa" in thresholds and saved.get("feature_policy") != FEATURE_POLICY):
            thresholds = {}
    thresholds = thresholds or {}
    config_lock = threading.RLock()
    if timeout <= 0 or not math.isfinite(timeout) or any(v < 0 or not math.isfinite(v) for v in thresholds.values()):
        raise ValueError("invalid timeout or calibration")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if source is not None and not db_path:
        raise ValueError("a job database path is required when a Prometheus source is configured")
    if jobs is not None and source is not None:
        jobs.bind_legacy_jobs({"source": source, "thresholds": thresholds, "stopping_policy": STOPPING_POLICY, "feature_policy": FEATURE_POLICY})
    slots = threading.BoundedSemaphore(concurrency)
    stop = threading.Event()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        worker = None
        if jobs is not None:
            worker = threading.Thread(target=work, args=(jobs, source, thresholds, timeout, slots, fetch, stop),
                                      name="corca-jobs", daemon=True)
            worker.start()
        yield
        stop.set()
        if worker is not None:
            worker.join(timeout=timeout * 3 + 10)

    # OpenAPI is served only behind the API key (see /openapi.json below).
    app = FastAPI(title="CoRCA", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimit)
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
    bearer = HTTPBearer(auto_error=False)  # Alertmanager sends "Authorization: Bearer <token>"

    def authorized(key: str | None = Depends(key_header), credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        supplied = key or (credentials.credentials if credentials else None)
        if supplied is None or not secrets.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "invalid API key")

    def require_jobs(method):
        if jobs is None or source is None:
            raise HTTPException(503, "Connect a telemetry source in Integrations first")
        if method not in thresholds:
            raise HTTPException(422, f"no calibrated tau for {method}; deploy with a calibration file")

    web = Path(__file__).parent / "web"
    landing, console = (web / "index.html").read_text(), (web / "app.html").read_text()

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return landing

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def app_page():
        return console

    @app.get("/v1/config", dependencies=[Depends(authorized)])
    def config():
        """What this server can do, so clients only offer working options."""
        with config_lock:
            return {"methods": sorted(thresholds), "prometheus": source is not None,
                    "alert_method": source["alert_method"] if source else None}

    def revision():
        return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()[:16] if source else None

    def current_context():
        return {"source": source, "thresholds": thresholds, "stopping_policy": STOPPING_POLICY, "feature_policy": FEATURE_POLICY}

    def check_revision(expected):
        if expected != revision():
            raise HTTPException(409, "The connection changed. Reload and discover services again.")

    @app.post("/v1/connections", dependencies=[Depends(authorized)])
    def connect(setup: ConnectionSetup):
        nonlocal source, thresholds
        if jobs is None:
            raise HTTPException(409, "Start the service with a persistent job database to save connections")
        try:
            candidate = validate_source(setup.source)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc))
        provider = candidate["provider"]
        hostname = urlsplit(candidate.get("prometheus_url", candidate.get("url"))).hostname
        allowed = {"localhost", "127.0.0.1", "::1"} | {h.strip() for h in os.getenv("CORCA_ALLOWED_SOURCE_HOSTS", "").split(",") if h.strip()}
        if source:
            allowed.add(urlsplit(source.get("prometheus_url", source.get("url", ""))).hostname)
        vendor = (provider in ("datadog", "newrelic") or
                  provider == "grafana" and hostname.endswith(".grafana.net") or
                  provider == "dynatrace" and hostname.endswith(".live.dynatrace.com"))
        if hostname not in allowed and not vendor:
            raise HTTPException(422, "Your administrator must add this telemetry hostname to CORCA_ALLOWED_SOURCE_HOSTS on the server")
        # Restrict web-managed credentials to telemetry-specific secrets, not arbitrary server environment variables.
        for field in ("token_env", "api_key_env", "app_key_env"):
            if field in candidate and not candidate[field].startswith("CORCA_TELEMETRY_"):
                raise HTTPException(422, "Credential variable names must start with CORCA_TELEMETRY_")
        with config_lock:
            check_revision(setup.revision)
            jobs.save_connection({"source": candidate, "thresholds": {}})
            source, thresholds = candidate, {}  # a changed source must not inherit unrelated calibration
        return {"connected": True, "revision": revision(), "calibration_required": True}

    @app.post("/v1/services/discover", dependencies=[Depends(authorized)])
    def discover():
        with config_lock:
            context, source_revision = current_context(), revision()
        configured = context["source"]
        if configured is None:
            raise HTTPException(409, "Connect a telemetry source first")
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "Service is busy. Try discovery again shortly.")
        try:
            end = time.time() - SCRAPE_LAG
            frame = fetch(configured, end - max(300, 12 * configured["step"]), end)
            return {**discover_services(frame, configured), "revision": source_revision, "checked_at": time.time()}
        except Exception:
            logging.exception("Service discovery failed")
            raise HTTPException(502, "Discovery failed. Check the endpoint, queries, service dimension, and credential environment variables on the server.")
        finally:
            slots.release()

    @app.post("/v1/topology", dependencies=[Depends(authorized)])
    def topology(setup: TopologySetup):
        nonlocal source, thresholds
        with config_lock:
            check_revision(setup.revision)
            if source is None or jobs is None:
                raise HTTPException(409, "Connect a telemetry source first")
            if any(a not in setup.services or b not in setup.services for a, b in setup.edges):
                raise HTTPException(422, "Dependencies must refer to selected services")
            try:
                candidate = validate_source({**source, "services": setup.services, "edges": [list(e) for e in setup.edges]})
            except (ValueError, TypeError, KeyError) as exc:
                raise HTTPException(422, str(exc))
            jobs.save_connection({"source": candidate, "thresholds": {}})
            source, thresholds = candidate, {}
        return {"saved": True, "revision": revision(), "calibration_required": True}

    @app.post("/v1/calibration", dependencies=[Depends(authorized)])
    def upload_calibration(setup: CalibrationSetup):
        nonlocal thresholds
        with config_lock:
            check_revision(setup.revision)
            if source is None or jobs is None:
                raise HTTPException(409, "Connect a telemetry source first")
            if "edges" not in source or not source.get("services"):
                raise HTTPException(422, "Confirm selected services and dependencies before loading calibration")
            try:
                candidate = calibration_thresholds(setup.calibration)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(422, "Upload a current CoRCA calibration artifact with methods, tau values, and matching stopping/feature policies")
            if not candidate:
                raise HTTPException(422, "Calibration has no supported methods")
            jobs.save_connection({"source": source, "thresholds": candidate})
            thresholds = candidate
        return {"loaded": True, "methods": sorted(thresholds)}

    @app.get("/v1/integrations", dependencies=[Depends(authorized)])
    def integrations():
        """Safe deployment metadata; never expose source URLs, queries, or credentials."""
        with config_lock:
            return {"provider": source.get("provider", "prometheus") if source else None,
                    "revision": revision(), "minimum_window_seconds": max(60, 10 * source["step"]) if source else 60,
                    "prometheus": {"configured": source is not None,
                                    "metric_kinds": sorted(source["queries"]) if source else [],
                                    "service_count": len(source.get("services") or []) if source else 0,
                                    "step_seconds": source["step"] if source else None,
                                    "topology": "custom" if source and "edges" in source else "online_boutique_default"},
                    "alertmanager": {"ready": bool(source and source["alert_method"] in thresholds),
                                     "method": source["alert_method"] if source else None,
                                     "lead_seconds": source["alert_lead_seconds"] if source else None},
                    "calibrated_methods": sorted(thresholds)}

    @app.post("/v1/integrations/check", dependencies=[Depends(authorized)])
    @app.post("/v1/integrations/prometheus/check", dependencies=[Depends(authorized)], deprecated=True)
    def check_prometheus():
        """Check the active source, not an arbitrary browser-supplied URL."""
        with config_lock:
            configured = source
        if configured is None:
            raise HTTPException(409, "Connect a telemetry source first.")
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "Service is busy. Try the connection check again shortly.")
        try:
            end = time.time() - SCRAPE_LAG
            frame = fetch(configured, end - max(300, 12 * configured["step"]), end)
            observed = frame.drop(columns=["time"], errors="ignore").dropna(axis=1, how="all")
            if observed.empty:
                raise HTTPException(422, "No usable recent metrics. Check queries, service labels, credentials, and reporting.")
            return {"status": "verified", "checked_at": time.time(), "metrics": len(observed.columns),
                    "services": len({service_of(c) for c in observed.columns})}
        except HTTPException:
            raise
        except Exception:
            logging.exception("Telemetry connection check failed")
            raise HTTPException(502, "Metrics could not be read. Ask your administrator to check the source URL, queries, and service logs.")
        finally:
            slots.release()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        if not thresholds:
            raise HTTPException(503, "no calibration loaded")
        if jobs is not None:
            jobs.recent(1)  # raises if the job database is unusable
        return {"status": "ok"}

    @app.get("/openapi.json", dependencies=[Depends(authorized)], include_in_schema=False)
    def openapi():
        return app.openapi()

    @app.post("/v1/diagnoses", status_code=202, dependencies=[Depends(authorized)])
    def submit(window: WindowRequest):
        """Queue a diagnosis with an immutable source/calibration snapshot."""
        with config_lock:
            require_jobs(window.method)
            minimum = 10 * source["step"]
            normal = window.normal_seconds if "normal_seconds" in window.model_fields_set else max(window.normal_seconds, minimum)
            anomaly = window.anomaly_seconds if "anomaly_seconds" in window.model_fields_set else max(window.anomaly_seconds, minimum)
            if min(normal, anomaly) < minimum:
                raise HTTPException(422, f"Each window needs at least {minimum} seconds for this source resolution")
            if window.inject_time > time.time() + 3600:
                raise HTTPException(422, "inject_time is more than an hour in the future")
            return jobs.add(window.method, window.inject_time, normal, anomaly, context=current_context())[0]

    @app.post("/v1/alerts", dependencies=[Depends(authorized)])
    def alert(hook: AlertmanagerWebhook):
        """Alertmanager webhook receiver: one diagnosis per alert group, source, and start time."""
        with config_lock:
            method = source["alert_method"] if source else "baro"
            require_jobs(method)
            firing = [a.startsAt.timestamp() for a in hook.alerts if a.status == "firing"]
            if hook.status != "firing" or not firing:
                return {"accepted": False, "reason": "no firing alerts"}
            inject_time = min(firing) - source["alert_lead_seconds"]
            minimum = 10 * source["step"]
            job, created = jobs.add(method, inject_time, max(300, minimum), max(120, minimum),
                                    dedupe=f"{revision()}:{hook.groupKey}@{inject_time:.0f}", context=current_context())
            return {"accepted": True, "duplicate": not created, **job}

    @app.get("/v1/diagnoses/{job_id}", dependencies=[Depends(authorized)])
    def get_diagnosis(job_id: str):
        if jobs is None or (job := jobs.get(job_id)) is None:
            raise HTTPException(404, "unknown diagnosis id")
        return job

    @app.get("/v1/diagnoses", dependencies=[Depends(authorized)])
    def list_diagnoses(limit: int = Query(20, ge=1, le=200)):
        return [] if jobs is None else jobs.recent(limit)

    @app.post("/diagnose", dependencies=[Depends(authorized)])
    def diagnosis(incident: Incident):
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "diagnosis capacity reached", headers={"Retry-After": "5"})
        started = time.perf_counter()
        try:
            result = diagnose(incident, thresholds, timeout)
            result["stopping_policy"] = STOPPING_POLICY
            if incident.method == "circa":
                result["feature_policy"] = FEATURE_POLICY
                result["graph_excluded_metrics"] = [c for c in incident.columns if c.endswith("_workload")]
            result["seconds"] = time.perf_counter() - started
            logging.info(json.dumps({"event": "diagnosis", "method": incident.method,
                                     "status": result["status"], "seconds": result["seconds"]}))
            return result
        except HTTPException:
            raise
        except Exception:
            logging.exception("diagnosis failed")
            raise HTTPException(503, "ranker failed; consult service logs")
        finally:
            slots.release()

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--calibration", default=os.getenv("CORCA_CALIBRATION"))
    parser.add_argument("--source", default=os.getenv("CORCA_SOURCE"), help="Bootstrap telemetry source JSON (saved workspace connection takes precedence)")
    parser.add_argument("--db", default=os.getenv("CORCA_DB", "runtime/.state/corca.db"))
    args = parser.parse_args()
    token_path = Path(os.getenv("CORCA_TOKEN_FILE", "runtime/.state/api-token"))
    token = os.getenv("CORCA_API_TOKEN")
    if not token:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if not token_path.exists():
            with token_path.open("x") as stream:
                os.chmod(token_path, 0o600)
                stream.write(secrets.token_urlsafe(32))
        token = token_path.read_text().strip()
    thresholds = None
    if args.calibration:
        thresholds = calibration_thresholds(json.loads(Path(args.calibration).read_text()))
    source = load_source(args.source) if args.source else None
    if source is not None:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(token, thresholds, source=source, db_path=args.db), host=args.host, port=args.port, timeout_keep_alive=5,
                limit_concurrency=8, timeout_graceful_shutdown=70)


if __name__ == "__main__":
    main()
