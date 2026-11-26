"""Bounded, authenticated incident diagnosis over metric windows."""
import argparse
import asyncio
import contextlib
import fcntl
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import networkx as nx
import numpy as np
import pandas as pd
import requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from corca import corca
from corca.base import anomaly_scores
from corca.circa import FEATURE_POLICY, InsufficientEvidence
from corca.corca import STOPPING_POLICY
from corca.topology import service_of, raw_service_of
from backend.telemetry import fetch_window, load_source, validate_source, discover_services
from backend.logs import MAX_LOG_BYTES, analyze_logs
from backend.credentials import Credentials, FIELDS as CREDENTIAL_FIELDS
from backend.quality import Evaluation, Evaluations
from backend.reviews import OutcomeReview, Reviews
from backend.evidence import capture_evidence
from backend.fusion import FUSION_POLICY, fuse
from backend.uncertainty import estimate as estimate_uncertainty
from corca.structural import repair_effects
from backend.helpbot import HelpBot, HelpQuestion
from backend.alerts import AlertInbox
from backend.access import Access, COOKIE, SESSION_SECONDS, audit


MAX_INCIDENT_ROWS = 2500
MAX_INCIDENT_COLUMNS = 200

class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    columns: list[str] = Field(min_length=2, max_length=MAX_INCIDENT_COLUMNS)
    rows: list[list[float | None]] = Field(min_length=20, max_length=MAX_INCIDENT_ROWS)
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


class TelemetryBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    environment: str = Field(min_length=1, max_length=100)
    incident: Incident
    traces: dict | list[dict] | None = None
    logs: dict | list[dict] | None = None

    @field_validator("environment")
    @classmethod
    def nonblank_environment(cls, value):
        if not value.strip():
            raise ValueError("Name the environment these observations came from")
        return value.strip()


class ConnectionSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: dict
    revision: str | None = None
    credentials: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    verify: bool = False


class TopologySetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    services: list[str] = Field(min_length=1, max_length=100)
    edges: list[tuple[str, str]] = Field(max_length=1000)


class CalibrationSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    calibration: dict


class SensitivitySetup(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    revision: str
    tau: float = Field(ge=0.1, le=1000)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    password: SecretStr = Field(min_length=1, max_length=256)


class UserCreate(LoginRequest):
    password: SecretStr = Field(min_length=15, max_length=256)
    role: Literal["administrator", "responder"] = "responder"


class UserAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["administrator", "responder"]
    enabled: bool


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=15, max_length=256)
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=256)


class AlertmanagerAlert(BaseModel):
    status: Literal["firing", "resolved"]
    startsAt: datetime
    endsAt: datetime | None = None
    fingerprint: str = Field(default="", max_length=128)
    labels: dict[str, Annotated[str, Field(max_length=512)]] = Field(default_factory=dict, max_length=64)
    annotations: dict[str, Annotated[str, Field(max_length=4096)]] = Field(default_factory=dict, max_length=64)

    @field_validator("startsAt", "endsAt")
    @classmethod
    def utc_timestamp(cls, value):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook payload (version 4); unknown fields are ignored."""
    status: Literal["firing", "resolved"]
    groupKey: str = Field(max_length=4096)
    alerts: list[AlertmanagerAlert] = Field(max_length=1000)


SCRAPE_LAG = 15        # seconds to wait after a window ends so Prometheus has the last samples
MAX_ATTEMPTS = 3       # transient Prometheus failures are retried; anything else fails at once
RETRY_SECONDS = 30


class Jobs:
    """Diagnosis jobs in SQLite: survive restarts, deduplicate alerts, keep history.

    shortcut: one application owner per local database; use shared job leases and a server database before scaling out.
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
            db.execute("CREATE TABLE IF NOT EXISTS diagnosis_evidence (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    @contextlib.contextmanager
    def worker_owner(self):
        path = Path(self.path).resolve()
        descriptor = os.open(path.with_name(path.name + ".worker.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another CoRCA instance owns this workspace; stop it before starting another") from None
            with self._db() as db:
                db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
            yield
        finally:
            # Keep the file: unlinking it lets another process lock a different inode.
            os.close(descriptor)

    @contextlib.contextmanager
    def _db(self):
        # Protect account/session hashes before SQLite creates or opens the workspace.
        # This shared path also covers local account CLI operations without restarting jobs.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
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

    def save_connection(self, context, audit_event=None):
        context = {**context, "stopping_policy": STOPPING_POLICY, "feature_policy": FEATURE_POLICY}
        with self._db() as db:
            db.execute("INSERT INTO settings(name,value) VALUES ('connection',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       (json.dumps(context),))
            if audit_event:
                audit(db, **audit_event)

    def add(self, method, inject_time, normal_seconds, anomaly_seconds, dedupe=None, context=None, max_pending=100):
        end = inject_time + anomaly_seconds
        job_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if dedupe:
                existing = db.execute("SELECT * FROM jobs WHERE dedupe = ?", (dedupe,)).fetchone()
                if existing is not None:
                    return self.view(existing), False
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= max_pending:
                raise HTTPException(503, "Diagnosis queue is full. Wait for pending jobs or cancel a queued diagnosis.",
                                    headers={"Retry-After": "30"})
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
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status = 'queued' AND ready_at <= ? ORDER BY ready_at LIMIT 1",
                             (time.time(),)).fetchone()
            if row is not None:
                db.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1 WHERE id = ?", (row["id"],))
                return {**dict(row), "attempts": row["attempts"] + 1}
        return None

    def cancel(self, job_id):
        with self._db() as db:
            changed = db.execute("UPDATE jobs SET status='cancelled', error=NULL WHERE id=? AND status='queued'",
                                 (job_id,)).rowcount
        return bool(changed)

    def finish(self, job_id, status, result=None, error=None, ready_at=None, evidence=None):
        with self._db() as db:
            db.execute("UPDATE jobs SET status = ?, result = ?, error = ?, ready_at = COALESCE(?, ready_at) WHERE id = ?",
                       (status, None if result is None else json.dumps(result), error, ready_at, job_id))
            if evidence is not None:
                db.execute('INSERT OR REPLACE INTO diagnosis_evidence VALUES (?,?)',(job_id,json.dumps(evidence,allow_nan=False)))

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
    started=time.perf_counter()
    try:
        context = json.loads(job.get("context") or "{}")
        if ("stopping_policy" in context and context["stopping_policy"] != STOPPING_POLICY or
                job["method"] == "circa" and "feature_policy" in context and context["feature_policy"] != FEATURE_POLICY):
            raise ValueError("Queued calibration policy is outdated; recalibrate before submitting a new diagnosis")
        source, thresholds = context.get("source", source), context.get("thresholds", thresholds)
        imported = context.get("imported_incident")
        frame = pd.DataFrame(imported["rows"], columns=imported["columns"]) if imported else fetch(source, job["start"], job["end"])
        fetched=time.perf_counter()
        rows = frame.astype(object).where(frame.notna(), None).values.tolist()
        incident = Incident.model_validate(imported) if imported else Incident(columns=list(frame.columns), rows=rows, inject_time=job["inject_time"],
                            method=job["method"], edges=source.get("edges"),
                            anomaly_threshold=thresholds.get(job["method"]) if context.get("threshold_mode") == "manual" else None)
        result = diagnose(incident, thresholds, timeout, dataset=None) if imported and incident.method=="baro" else diagnose(incident, thresholds, timeout)
        result["threshold_mode"] = context.get("threshold_mode", "calibrated")
        fusion = context.get("fusion")
        if fusion:
            prepared, dropped, _ = prepare_window(incident)
            result["fusion"] = {k:v for k,v in fusion.items() if k != "modalities"}
            result["fusion"]["used_modalities"] = sorted({m for c,m in fusion["modalities"].items() if c not in dropped})
            result["fusion"]["discarded_columns"] = dropped
            result["structural_model"] = repair_effects(prepared,incident.inject_time,incident.edges or [],result.get("root_causes",[]))
            result["analysis_profile"] = context["analysis_profile"]
            result["input_kind"] = "uploaded_telemetry"
        analyzed=time.perf_counter()
        evidence=capture_evidence(frame,source,job['inject_time'],fusion["modalities"] if fusion else None)
        if fusion:
            evidence["fusion"] = fusion
        result['processing_seconds']=time.perf_counter()-started
        result['timings']={'fetch_seconds':fetched-started,'analysis_seconds':analyzed-fetched,
                           'preview_seconds':time.perf_counter()-analyzed}
        result['time_to_result_seconds']=time.time()-job['created'] if time.time()>=job['created'] else None
        jobs.finish(job["id"], "done", result=result, evidence=evidence)
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


def diagnose(incident, thresholds, timeout, dataset="re2-ob"):
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
    if incident.anomaly_threshold is not None and (scores.empty or scores.max() <= incident.anomaly_threshold):
        return {"status": "no_significant_anomaly", "root_causes": [], "rounds": [],
                "dropped_metrics": unusable, "anomaly_threshold": incident.anomaly_threshold}
    try:
        result = corca(df, incident.inject_time, method=incident.method, graph=graph,
                       max_rounds=3, tau=tau, round_timeout=timeout, dataset=dataset)
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


def create_app(token=None, thresholds=None, timeout=20, concurrency=2, source=None, db_path=None, fetch=fetch_window, max_pending=100, require_accounts=False, help_bot=None, evaluation_token=None):
    if evaluation_token is not None and (len(evaluation_token)<16 or evaluation_token==token):
        raise ValueError("Use a separate evaluation publisher token of at least 16 characters")
    if token is not None and len(token) < 16:
        raise ValueError("API token must be at least 16 characters")
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    jobs = Jobs(db_path) if db_path else None
    inbox = AlertInbox(jobs) if jobs else None
    vault = Credentials(db_path) if db_path else None
    access = Access(jobs) if jobs else None
    if require_accounts and not (access and access.enabled()):
        raise ValueError("Individual access is required. Restore an account-enabled workspace or bootstrap its first administrator locally.")
    evaluations = Evaluations(jobs) if jobs else None
    reviews = Reviews(jobs) if jobs else None
    customer_help = help_bot if help_bot is not None else HelpBot()
    provider_fetch = fetch

    def fetch(configured, start, end):
        values = vault.read(configured) if vault else {}
        try:
            return provider_fetch({**configured, "_credential_values": values} if values else configured, start, end)
        except requests.RequestException:
            if values:
                raise requests.RequestException("Saved-credential telemetry request failed") from None
            raise
        except Exception:
            if values:
                raise ValueError("Saved-credential telemetry query failed") from None
            raise

    saved = jobs.connection() if jobs else None
    if saved:
        source = validate_source(saved["source"])
        thresholds = saved["thresholds"]
        if (saved.get("stopping_policy") != STOPPING_POLICY or
                "circa" in thresholds and saved.get("feature_policy") != FEATURE_POLICY):
            thresholds = {}
    thresholds = thresholds or {}
    threshold_mode = saved.get("threshold_mode", "calibrated") if saved else "calibrated"
    config_lock = threading.RLock()
    if timeout <= 0 or not math.isfinite(timeout) or any(v < 0 or not math.isfinite(v) for v in thresholds.values()):
        raise ValueError("invalid timeout or calibration")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
        raise ValueError("max_pending must be a positive integer")
    if source is not None and not db_path:
        raise ValueError("a job database path is required when a Prometheus source is configured")
    if jobs is not None and source is not None:
        jobs.bind_legacy_jobs({"source": source, "thresholds": thresholds, "stopping_policy": STOPPING_POLICY, "feature_policy": FEATURE_POLICY})
    slots = threading.BoundedSemaphore(concurrency)
    stop = threading.Event()
    worker = None

    @contextlib.asynccontextmanager
    async def lifespan(app):
        nonlocal worker
        if jobs is None:
            yield
            return
        with jobs.worker_owner():
            stop.clear()
            worker = threading.Thread(target=work, args=(jobs, source, thresholds, timeout, slots, fetch, stop),
                                      name="corca-jobs", daemon=True)
            try:
                worker.start()
                yield
            finally:
                stop.set()
                if worker.ident is not None:
                    # Ownership must outlive active work, including during shutdown.
                    await run_in_threadpool(worker.join)

    # OpenAPI is served only behind the API key (see /openapi.json below).
    app = FastAPI(title="CoRCA", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimit)

    @app.exception_handler(RequestValidationError)
    async def connection_validation(request, exc):
        if request.url.path.endswith('/review'):
            return JSONResponse({'detail':'Invalid outcome review. Check causes, evidence, and revision; include no secrets.'},status_code=422)
        if request.url.path != "/v1/connections" and not request.url.path.startswith("/v1/access/") and request.url.path not in ("/v1/help/ask", "/v1/evaluations"):
            return await request_validation_exception_handler(request, exc)
        # Validation errors must not echo a credential-bearing request body.
        return JSONResponse({"detail": "Invalid connection fields. Check the provider and credential values." if request.url.path == "/v1/connections" else "Invalid evaluation summary. Check its schema, metrics, and timestamps; include no secrets." if request.url.path == "/v1/evaluations" else "Enter a question of 1–2,000 characters and at most six recent messages." if request.url.path == "/v1/help/ask" else "Invalid account fields. Check the username and password requirements."}, status_code=422)


    @app.middleware("http")
    async def response_headers(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin workspace changes are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
    bearer = HTTPBearer(auto_error=False)  # Alertmanager sends "Authorization: Bearer <token>"

    def authorized(request: Request, key: str | None = Depends(key_header), credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        managed = bool(access and access.enabled())
        actor = access.resolve(request.cookies.get(COOKIE)) if managed else None
        supplied = key or (credentials.credentials if credentials else None)
        if actor is None and evaluation_token and supplied and secrets.compare_digest(supplied.encode(), evaluation_token.encode()):
            actor = {"id":"evaluation-pipeline","name":"Evaluation pipeline","role":"evaluation_service"}
        if actor is None and token and supplied and secrets.compare_digest(supplied.encode(), token.encode()):
            actor = {"id":"alertmanager-service" if managed else "legacy-key", "name":"Alertmanager service" if managed else "Shared API key", "role":"service" if managed else "administrator"}
        if actor is None and not managed and token is None:
            actor = {"id":"local-workspace", "name":"Unidentified local user", "role":"administrator"}
        if actor is None:
            raise HTTPException(401, "Sign in to access this workspace" if managed else "invalid API key")
        if actor["role"] == "service" and not (request.method == "POST" and request.url.path == "/v1/alerts"):
            raise HTTPException(403, "The service token is restricted to Alertmanager delivery")
        if actor["role"] == "evaluation_service" and not (request.method == "POST" and request.url.path == "/v1/evaluations"):
            raise HTTPException(403, "The evaluation token is restricted to publishing summaries")
        request.state.actor = actor
        return actor

    def administrator(actor=Depends(authorized)):
        if actor["role"] != "administrator":
            raise HTTPException(403, "Administrator access required")
        return actor

    def account_administrator(actor=Depends(administrator)):
        if not access or not access.enabled():
            raise HTTPException(409, "Create the first administrator on the deployment host with backend.access bootstrap-admin")
        return actor

    def configuration_audit(actor, action, candidate, **details):
        return {"actor":actor,"action":action,"target":"workspace", "details":{
            "provider":candidate.get("provider", "prometheus"),"service_count":len(candidate.get("services") or []),
            "previous_revision":revision(),"revision":hashlib.sha256(json.dumps(candidate,sort_keys=True).encode()).hexdigest()[:16],
            "previous_thresholds":dict(thresholds), **details}}

    @app.get("/v1/access/session")
    def session(request: Request):
        managed = bool(access and access.enabled())
        actor = access.resolve(request.cookies.get(COOKIE)) if managed else None
        return {"enabled":managed, "user":actor, "session_seconds":SESSION_SECONDS}

    @app.post("/v1/access/login")
    def login(body: LoginRequest, request: Request):
        if not access or not access.enabled():
            raise HTTPException(409, "Individual access is not enabled")
        if request.url.scheme != "https" and request.url.hostname not in ("localhost","127.0.0.1","::1"):
            raise HTTPException(400, "Use HTTPS to sign in to a remote deployment")
        session_token, actor = access.login(body.username,body.password.get_secret_value(), request.client.host if request.client else "unknown")
        previous = request.cookies.get(COOKIE)
        if previous and (previous_actor := access.resolve(previous)):
            access.logout(previous,previous_actor)
        response = JSONResponse({"user":actor})
        response.set_cookie(COOKIE,session_token,max_age=SESSION_SECONDS,httponly=True,secure=request.url.scheme == "https",samesite="strict",path="/")
        return response

    @app.post("/v1/access/logout")
    def logout(request: Request, actor=Depends(authorized)):
        if access and request.cookies.get(COOKIE):
            access.logout(request.cookies[COOKIE],actor)
        response = JSONResponse({"signed_out":True}); response.delete_cookie(COOKIE,path="/",samesite="strict")
        return response

    @app.get("/v1/access/users")
    def list_users(actor=Depends(account_administrator)):
        return access.users()

    @app.post("/v1/access/users", status_code=201)
    def create_user(body: UserCreate, actor=Depends(account_administrator)):
        return access.create_user(body.username,body.password.get_secret_value(),body.role,actor)

    @app.patch("/v1/access/users/{identifier}")
    def change_user(identifier: str, body: UserAccess, actor=Depends(account_administrator)):
        access.change_user(identifier,body.role,body.enabled,actor)
        return {"saved":True}

    @app.post("/v1/access/users/{identifier}/password")
    def change_password(identifier: str, body: PasswordChange, actor=Depends(authorized)):
        if not access or not access.enabled():
            raise HTTPException(409, "Individual access is not enabled")
        if actor["id"] != identifier and actor["role"] != "administrator":
            raise HTTPException(403, "Administrator access required")
        if actor["id"] == identifier and body.current_password is None:
            raise HTTPException(422, "Enter your current password")
        access.change_password(identifier,body.password.get_secret_value(),actor,
                               body.current_password.get_secret_value() if body.current_password else None)
        return {"saved":True,"sign_in_required":True}

    @app.get("/v1/access/audit")
    def audit_events(before: int | None = Query(default=None,gt=0), limit: int = Query(default=100,ge=1,le=200), actor=Depends(account_administrator)):
        events = access.events(before,limit)
        return {"events":events,"next_before":events[-1]["id"] if len(events)==limit else None}

    @app.get("/v1/evaluations", dependencies=[Depends(authorized)])
    def list_evaluations(kind: Literal["all","diagnosis","help","release"] = "diagnosis", before: int | None = Query(default=None,gt=0)):
        if evaluations is None:
            raise HTTPException(503,"Start with persistent storage to retain evaluation evidence")
        return evaluations.recent(kind,before)

    @app.get('/v1/diagnosis-evaluations', dependencies=[Depends(authorized)])
    def diagnosis_evaluations(before: int | None = Query(default=None,gt=0)):
        if reviews is None: raise HTTPException(503,'Persistent storage is required for outcome reviews')
        return reviews.summary(before)

    @app.get('/v1/diagnoses/{job_id}/review', dependencies=[Depends(authorized)])
    def diagnosis_review(job_id: str):
        if reviews is None: raise HTTPException(503,'Persistent storage is required for outcome reviews')
        return {'review':reviews.get(job_id)}

    @app.get('/v1/diagnoses/{job_id}/uncertainty', dependencies=[Depends(authorized)])
    def diagnosis_uncertainty(job_id: str):
        if jobs is None:
            raise HTTPException(503, "Persistent storage is required for confirmed-outcome calibration")
        try:
            return estimate_uncertainty(jobs, job_id)
        except KeyError:
            raise HTTPException(404, "Diagnosis not found") from None

    @app.get('/v1/diagnoses/{job_id}/evidence', dependencies=[Depends(authorized)])
    def diagnosis_evidence(job_id: str):
        if jobs is None or jobs.get(job_id) is None: raise HTTPException(404,'Diagnosis not found')
        with jobs._db() as db:
            row=db.execute('SELECT payload FROM diagnosis_evidence WHERE job_id=?',(job_id,)).fetchone()
            context=db.execute('SELECT context FROM jobs WHERE id=?',(job_id,)).fetchone()[0]
            alerts=[dict(a) for a in db.execute('SELECT name,service,status,starts_at FROM alert_events WHERE job_id=? ORDER BY starts_at LIMIT 20',(job_id,))]
        source=(json.loads(context).get('source') or {}) if context else {}
        evidence=json.loads(row['payload']) if row else {'available':False,'series':[],
            'edges':source.get('edges',[]),'scope':source.get('services',[]),
            'note':'Chart evidence was not captured for this diagnosis. New diagnoses capture a bounded preview.'}
        return {'scope':source.get('services',[]),**evidence,'alerts':alerts}

    @app.post('/v1/diagnoses/{job_id}/review')
    def review_diagnosis(job_id: str, body: OutcomeReview, actor=Depends(authorized)):
        if reviews is None: raise HTTPException(503,'Persistent storage is required for outcome reviews')
        return {'review':reviews.save(job_id,body,actor)}

    @app.post("/v1/evaluations", status_code=201)
    def publish_evaluation(body: Evaluation, actor=Depends(authorized)):
        if actor["role"] not in ("administrator","evaluation_service"):
            raise HTTPException(403,"Administrator or evaluation publisher access required")
        if evaluations is None:
            raise HTTPException(503,"Start with persistent storage to retain evaluation evidence")
        return evaluations.publish(body,actor)

    @app.get("/v1/help", dependencies=[Depends(authorized)])
    def help_metadata():
        return customer_help.metadata()

    @app.post("/v1/help/ask")
    def ask_help(body: HelpQuestion, actor=Depends(authorized)):
        with config_lock:
            context = {"current_view":body.view, "role":actor["role"],
                       "individual_access":bool(access and access.enabled()),
                       "monitoring_connected":source is not None,
                       "provider":source.get("provider", "prometheus") if source else None,
                       "service_count":len(source.get("services") or []) if source else 0,
                       "methods":sorted(thresholds)}
        return customer_help.ask(body,context,actor["id"])

    def require_jobs(method):
        if jobs is None or source is None:
            raise HTTPException(503, "Connect a telemetry source in Setup → Connect & verify first")
        if method not in thresholds:
            raise HTTPException(422, f"Configure sensitivity or load calibration for {method} on the Services page")

    web = Path(__file__).parent.parent / "frontend"
    landing, console = (web / "index.html").read_text(), (web / "app.html").read_text()
    app.mount("/assets/fonts", StaticFiles(directory=web / "fonts"), name="console-fonts")

    @app.get('/assets/workspace-visuals.js',include_in_schema=False)
    def workspace_visuals():
        return FileResponse(web/'workspace-visuals.js',media_type='application/javascript')

    @app.get('/assets/workspace-visuals.css',include_in_schema=False)
    def workspace_visual_styles():
        return FileResponse(web/'workspace-visuals.css',media_type='text/css')

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return landing

    console = console.replace("__CORCA_APP_VERSION__", app.version)

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def app_page():
        return console

    @app.post("/v1/logs/analyze", dependencies=[Depends(authorized)])
    async def log_analysis(request: Request, filename: str = Query(min_length=1, max_length=255),
                           service: str = Query(default="", max_length=128),
                           incident_start: float | None = Query(default=None, ge=0, le=253402300799, allow_inf_nan=False)):
        """Analyze UTF-8 log bytes without storing the upload or report."""
        if Path(filename).suffix.lower() not in (".log", ".txt", ".jsonl", ".ndjson"):
            raise HTTPException(422, "Use a .log, .txt, .jsonl, or .ndjson file.")
        body = await request.body()
        if len(body) > MAX_LOG_BYTES:
            raise HTTPException(413, "Choose a log file no larger than 2 MB.")
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "Service is busy. Try log analysis again shortly.")
        try:
            return await run_in_threadpool(analyze_logs, body, Path(filename).name, service, incident_start)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            slots.release()

    @app.get("/v1/config", dependencies=[Depends(authorized)])
    def config():
        """What this server can do, so clients only offer working options."""
        with config_lock:
            return {"methods": sorted(thresholds), "prometheus": source is not None,
                    "alert_method": source["alert_method"] if source else None}

    def revision():
        return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()[:16] if source else None

    def current_context():
        return {"source": source, "thresholds": thresholds, "threshold_mode": threshold_mode,
                "stopping_policy": STOPPING_POLICY, "feature_policy": FEATURE_POLICY}

    def check_revision(expected):
        if expected != revision():
            raise HTTPException(409, "The connection changed. Reload and discover services again.")

    @app.post("/v1/connections", dependencies=[Depends(administrator)])
    def connect(setup: ConnectionSetup, actor=Depends(administrator)):
        nonlocal source, thresholds
        if jobs is None:
            raise HTTPException(409, "Start the service with a persistent job database to save connections")
        identifier, values = None, {}
        try:
            candidate_source = dict(setup.source)
            if setup.credentials:
                identifier, references, values = vault.prepare(candidate_source.get("provider", "prometheus"), setup.credentials)
                for field in CREDENTIAL_FIELDS.values():
                    candidate_source.pop(field, None)
                candidate_source.update(references)
            candidate = validate_source(candidate_source)
            if values and urlsplit(candidate.get("prometheus_url", candidate.get("url"))).scheme != "https" and urlsplit(candidate.get("prometheus_url", candidate.get("url"))).hostname not in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
                raise ValueError("Use HTTPS when sending credentials to a remote telemetry endpoint")
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
        inventory, verification = None, None
        if setup.verify or values:
            if not slots.acquire(blocking=False):
                raise HTTPException(503, "Service is busy. Try connecting again shortly.")
            try:
                end = time.time() - SCRAPE_LAG
                transient = {**candidate, "_credential_values": values} if values else candidate
                frame = provider_fetch(transient, end - max(300, 12 * candidate["step"]), end) if values else fetch(candidate, end - max(300, 12 * candidate["step"]), end)
                inventory = discover_services(frame, candidate)
                observed = frame.drop(columns=["time"], errors="ignore").dropna(axis=1, how="all")
                if not inventory["services"] or observed.empty:
                    raise ValueError("No service metrics")
                verification = {"status": "verified", "checked_at": time.time(), "metrics": len(observed.columns), "services": len(inventory["services"])}
            except Exception as exc:
                logging.warning("Connection verification failed (%s)", type(exc).__name__)
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in (401, 403):
                    raise HTTPException(422, "The provider rejected these API credentials. Check the key/token and its read permissions.") from None
                raise HTTPException(422, "Could not verify service metrics. Check the endpoint, credentials, and optional Metric settings.") from None
            finally:
                slots.release()
        with config_lock:
            check_revision(setup.revision)
            credential_file = None
            try:
                if values:
                    credential_file = vault.save(identifier, values)
                jobs.save_connection({"source": candidate, "thresholds": {}}, configuration_audit(actor,"connection.saved",candidate))
            except (OSError, ValueError, sqlite3.Error):
                if credential_file:
                    credential_file.unlink(missing_ok=True)
                raise HTTPException(503, "Could not save this connection. Check persistent storage and credential-file permissions.") from None
            source, thresholds = candidate, {}
            saved_revision = revision()
        result = {"connected": True, "revision": saved_revision, "calibration_required": True}
        if verification:
            result["verification"] = verification
            result["discovery"] = {**inventory, "revision": saved_revision, "checked_at": verification["checked_at"], "selected_services": candidate.get("services", [])}
        return result

    @app.get("/v1/services", dependencies=[Depends(authorized)])
    def saved_services():
        """Saved service scope survives reloads; coverage requires an explicit live check."""
        with config_lock:
            if source is None:
                return {"services": [], "revision": None, "selected_services": [], "checked_at": None}
            inventory = discover_services(pd.DataFrame({"time": []}), source)
            for service in inventory["services"]:
                service["status"] = "unverified"
            return {**inventory, "revision": revision(), "selected_services": source.get("services", []),
                    "checked_at": None}

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
            frame = fetch({**configured, "services": []}, end - max(300, 12 * configured["step"]), end)
            return {**discover_services(frame, configured), "revision": source_revision, "checked_at": time.time(),
                    "selected_services": configured.get("services", [])}
        except Exception:
            logging.exception("Service discovery failed")
            raise HTTPException(502, "Discovery failed. Check the endpoint, queries, service dimension, and credential environment variables on the server.")
        finally:
            slots.release()

    @app.post("/v1/topology", dependencies=[Depends(administrator)])
    def topology(setup: TopologySetup, actor=Depends(administrator)):
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
            jobs.save_connection({"source": candidate, "thresholds": {}}, configuration_audit(actor,"services.saved",candidate, dependency_count=len(candidate["edges"])))
            source, thresholds = candidate, {}
        return {"saved": True, "revision": revision(), "calibration_required": True}

    @app.post("/v1/calibration", dependencies=[Depends(administrator)])
    def upload_calibration(setup: CalibrationSetup, actor=Depends(administrator)):
        nonlocal thresholds, threshold_mode
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
            jobs.save_connection({"source": source, "thresholds": candidate}, configuration_audit(actor,"calibration.saved",source, methods=sorted(candidate)))
            thresholds = candidate
            threshold_mode = "calibrated"
        return {"loaded": True, "methods": sorted(thresholds)}

    @app.post("/v1/sensitivity", dependencies=[Depends(administrator)])
    def sensitivity(setup: SensitivitySetup, actor=Depends(administrator)):
        """Enable BARO investigation with an explicitly manual stopping threshold."""
        nonlocal thresholds, threshold_mode
        with config_lock:
            check_revision(setup.revision)
            if source is None or jobs is None or not source.get("services") or "edges" not in source:
                raise HTTPException(409, "Save selected services and dependencies first")
            jobs.save_connection({**current_context(), "thresholds": {"baro": setup.tau}, "threshold_mode": "manual"}, configuration_audit(actor,"sensitivity.saved",source, threshold=setup.tau, mode="manual"))
            thresholds, threshold_mode = {"baro": setup.tau}, "manual"
        return {"saved": True, "methods": ["baro"], "threshold_mode": "manual", "tau": setup.tau}

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
                    "threshold_mode": threshold_mode if thresholds else None,
                    "thresholds": thresholds,
                    "queue_limit": max_pending,
                    "calibrated_methods": sorted(thresholds) if threshold_mode == "calibrated" else []}

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
        # Setup must remain reachable through an orchestrator before a source is configured.
        if jobs is not None:
            try:
                jobs.recent(1)
            except (OSError, sqlite3.Error, ValueError):
                logging.exception("Workspace readiness check failed")
                raise HTTPException(503, "Workspace storage is unavailable") from None
            if worker is None or not worker.is_alive():
                raise HTTPException(503, "Diagnosis worker is unavailable")
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
            return jobs.add(window.method, window.inject_time, normal, anomaly,
                            context=current_context(), max_pending=max_pending)[0]

    @app.post("/v1/diagnoses/import", status_code=202, dependencies=[Depends(authorized)])
    def import_diagnosis(bundle: TelemetryBundle):
        """Queue aligned numeric evidence; raw log bodies and spans are not retained."""
        if jobs is None:
            raise HTTPException(503, "Start with persistent storage to import investigations")
        incident = bundle.incident
        if incident.tau is None:
            raise HTTPException(422, "Set an explicit sensitivity for the combined telemetry; metric-only calibration is not interchangeable")
        if incident.edges is None and bundle.traces is None:
            raise HTTPException(422, "Provide trace evidence or dependencies (an empty list confirms independent services)")
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "Diagnosis capacity reached", headers={"Retry-After":"5"})
        try:
            frame = pd.DataFrame(incident.rows, columns=incident.columns)
            if frame["time"].iloc[0] <= 0 or frame["time"].iloc[-1] > time.time():
                raise ValueError("Imported observations must use completed positive Unix-time windows")
            combined, fusion = fuse(frame, incident.inject_time, bundle.traces, bundle.logs)
            edges = sorted(set(incident.edges or []) | {tuple(e) for e in fusion["topology"]["edges"]})
            payload = {**incident.model_dump(), "columns":list(combined.columns),
                       "rows":combined.astype(object).where(combined.notna(),None).values.tolist(),
                       "edges":edges, "anomaly_threshold":incident.tau}
            checked = Incident.model_validate(payload)
            config = {"environment":bundle.environment, "columns":sorted(combined.columns),
                      "method":incident.method, "tau":incident.tau, "policy":FUSION_POLICY,
                      "stopping_policy":STOPPING_POLICY,"feature_policy":FEATURE_POLICY,
                      "step_seconds":float(np.median(np.diff(frame["time"]))),
                      "topology_policy":"configured_plus_observed" if bundle.traces is not None else "configured",
                      "configured_edges":sorted(incident.edges or [])}
            analysis_profile = hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
            context = {"source":{"provider":"uploaded","services":sorted({service_of(c) for c in combined if c!="time"}),"edges":edges},
                       "thresholds":{incident.method:incident.tau}, "threshold_mode":"manual",
                       "stopping_policy":STOPPING_POLICY,"feature_policy":FEATURE_POLICY,
                       "imported_incident":checked.model_dump(),"fusion":fusion,"analysis_profile":analysis_profile}
            return jobs.add(incident.method,incident.inject_time,
                            incident.inject_time-float(frame["time"].iloc[0]),
                            float(frame["time"].iloc[-1])-incident.inject_time,
                            context=context,max_pending=max_pending)[0]
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise HTTPException(422, "Invalid telemetry bundle: "+str(exc)[:240]) from None
        finally:
            slots.release()

    @app.get("/v1/alerts", dependencies=[Depends(authorized)])
    def list_alerts(status: Literal["all", "firing", "resolved"] = "all", q: str = Query(default="", max_length=128)):
        if inbox is None:
            raise HTTPException(503, "Start the service with a persistent database to receive alerts")
        with config_lock:
            return {**inbox.recent(status, q), "receiver_ready": True,
                    "diagnoses_ready": bool(source and source["alert_method"] in thresholds), "requires_auth": token is not None, "service_token_required": bool(access and access.enabled()), "service_token_configured": token is not None}

    def receive_alerts(hook, origin="alertmanager"):
        if inbox is None:
            raise HTTPException(503, "Start the service with a persistent database to receive alerts")
        firing = [a for a in hook.alerts if a.status == "firing"] if hook.status == "firing" else []
        with config_lock:
            method = source["alert_method"] if source else "baro"
            lead = source["alert_lead_seconds"] if source else 60
            for alert in firing:
                started = alert.startsAt.timestamp()
                if started - lead <= 0 or started > time.time() + 3600:
                    raise HTTPException(422, "Alert start must be a positive Unix timestamp no more than an hour in the future, with enough history for its lead")
            # Keep only each episode's final effective state if a payload repeats it.
            events = list({event["id"]: event for event in inbox.record(hook, origin)}.values())
            if origin == "console_test":
                return {"accepted": True, "test_only": True, "external_delivery_verified": False}
            events = [event for event in events if event["firing"]]
            if not events:
                return {"accepted": False, "reason": "no firing alerts"}
            affected, pending = {}, []
            for event in events:
                if event["job_id"]:
                    job = jobs.get(event["job_id"])
                    if job is None:
                        raise HTTPException(503, "A saved alert diagnosis is unavailable; check workspace integrity")
                    affected[job["id"]] = job
                else:
                    pending.append(event)
            created_any = False
            if pending:
                identifiers = [event["id"] for event in pending]
                try:
                    require_jobs(method)
                except HTTPException:
                    inbox.link(identifiers, error="Connect monitoring, save services, and set sensitivity to enable automatic diagnoses.")
                    raise
                step = source["step"]
                normal = max(300, 10 * step)
                # Respect the configured lead and retain at least ten post-alert samples.
                anomaly = max(120, 10 * step, lead + 10 * step)
                groups = []
                for event in sorted(pending, key=lambda event: (event["starts_at"], event["id"])):
                    first_alert_sample = math.ceil(event["starts_at"] / step)
                    if groups and first_alert_sample <= groups[-1]["last_alert_sample"]:
                        groups[-1]["events"].append(event)
                        continue
                    inject_time = event["starts_at"] - lead
                    first_sample = math.ceil((inject_time - normal) / step)
                    last_sample = math.floor((inject_time + anomaly) / step)
                    split_sample = math.ceil(inject_time / step)
                    columns = len(source.get("services") or []) * len(source["queries"]) + 1
                    if (inject_time - normal < 0 or first_sample > last_sample
                            or last_sample - first_sample + 1 > MAX_INCIDENT_ROWS
                            or min(split_sample - first_sample, last_sample - split_sample + 1) < 10
                            or columns > MAX_INCIDENT_COLUMNS):
                        message = "Alert analysis window exceeds supported samples or service columns. Reduce alert lead/service scope or use a coarser query step."
                        inbox.link(identifiers, error=message)
                        raise HTTPException(422, message)
                    # An episode fits only if its aligned onset still has ten samples
                    # through this window's last point. Distant episodes need new jobs.
                    groups.append({"events": [event], "inject_time": inject_time,
                                   "last_alert_sample": last_sample - 9})
                for index, group in enumerate(groups):
                    try:
                        job, created = jobs.add(method, group["inject_time"], normal, anomaly,
                                                dedupe="alert:" + group["events"][0]["id"],
                                                context=current_context(), max_pending=max_pending)
                    except HTTPException:
                        remaining = [event["id"] for remaining in groups[index:] for event in remaining["events"]]
                        inbox.link(remaining, error="Diagnosis queue is full; delivery can retry.")
                        raise
                    inbox.link([event["id"] for event in group["events"]], job_id=job["id"])
                    affected[job["id"]] = job
                    created_any = created_any or created
            # Retain the single-job contract; multiple groups add bounded, safe job metadata.
            ordered = sorted(affected.values(), key=lambda job: (job["inject_time"], job["id"]))
            result = {"accepted": True, "duplicate": not created_any, **ordered[-1]}
            if len(ordered) > 1:
                result["jobs"] = [{key: job[key] for key in ("id", "status", "method", "inject_time", "window")} for job in ordered]
            return result

    @app.post("/v1/alerts", dependencies=[Depends(authorized)])
    def alert(hook: AlertmanagerWebhook, actor=Depends(authorized)):
        """Persist received alert state and deduplicate the associated diagnosis."""
        if access and access.enabled() and actor["role"] != "service":
            raise HTTPException(403, "Use the Alertmanager service token for webhook delivery")
        return receive_alerts(hook)

    @app.post("/v1/alerts/test", dependencies=[Depends(administrator)])
    def test_alert_receiver():
        """Local receiver/storage test only; no diagnosis or external delivery claim."""
        now = datetime.now(timezone.utc)
        hook = AlertmanagerWebhook(status="resolved", groupKey="corca-console-test-" + uuid.uuid4().hex,
             alerts=[AlertmanagerAlert(status="resolved", startsAt=now, endsAt=now,
                     labels={"alertname": "CoRCA receiver test", "severity": "info"},
                     annotations={"summary": "Local receiver test completed; no diagnosis was queued."})])
        return receive_alerts(hook, "console_test")

    @app.post("/v1/diagnoses/{job_id}/cancel", dependencies=[Depends(authorized)])
    def cancel_diagnosis(job_id: str):
        if jobs is None or jobs.get(job_id) is None:
            raise HTTPException(404, "unknown diagnosis id")
        if not jobs.cancel(job_id):
            raise HTTPException(409, "Only queued diagnoses can be cancelled. Analysis may already have started.")
        return jobs.get(job_id)

    @app.post("/v1/diagnoses/{job_id}/rerun", status_code=202, dependencies=[Depends(authorized)])
    def rerun_saved_diagnosis(job_id: str):
        if jobs is None:
            raise HTTPException(404, "Diagnosis not found")
        with jobs._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Diagnosis not found")
        if row["status"] in ("queued","running"):
            raise HTTPException(409, "Wait for this investigation to finish")
        context = json.loads(row["context"] or "{}")
        if not context:
            raise HTTPException(409, "This legacy diagnosis has no saved input configuration")
        return jobs.add(row["method"],row["inject_time"],row["inject_time"]-row["start"],
                        row["end"]-row["inject_time"],context=context,max_pending=max_pending)[0]

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
            result["threshold_mode"] = "manual" if incident.tau is not None or incident.anomaly_threshold is not None else threshold_mode
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
    parser.add_argument("--db", default=os.getenv("CORCA_DB", "backend/.state/corca.db"))
    parser.add_argument("--max-pending", type=int, default=int(os.getenv("CORCA_MAX_PENDING", "100")))
    args = parser.parse_args()
    token = os.getenv("CORCA_API_TOKEN")
    if not token and os.getenv("CORCA_TOKEN_FILE"):
        token = Path(os.environ["CORCA_TOKEN_FILE"]).read_text().strip()
    evaluation_token = os.getenv("CORCA_EVALUATION_TOKEN")
    if not evaluation_token and os.getenv("CORCA_EVALUATION_TOKEN_FILE"):
        evaluation_token = Path(os.environ["CORCA_EVALUATION_TOKEN_FILE"]).read_text().strip()
    thresholds = None
    if args.calibration:
        thresholds = calibration_thresholds(json.loads(Path(args.calibration).read_text()))
    source = load_source(args.source) if args.source else None
    if source is not None:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(token, thresholds, source=source, db_path=args.db, max_pending=args.max_pending, evaluation_token=evaluation_token, require_accounts=os.getenv("CORCA_REQUIRE_ACCOUNTS", "0") == "1"), host=args.host, port=args.port, timeout_keep_alive=5,
                # Includes idle browser keep-alives; expensive work has separate admission limits.
                limit_concurrency=64, timeout_graceful_shutdown=70)


if __name__ == "__main__":
    main()
