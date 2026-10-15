"""Bounded, authenticated incident diagnosis over metric windows."""
import argparse
import asyncio
import json
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import networkx as nx
import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, model_validator

from corca import corca
from corca.base import anomaly_scores
from corca.circa import FEATURE_POLICY, InsufficientEvidence
from corca.corca import STOPPING_POLICY
from corca.topology import service_of


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
        graph = nx.DiGraph(incident.edges)
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


def create_app(token, thresholds=None, timeout=20, concurrency=2):
    if not token or len(token) < 16:
        raise ValueError("API token must be at least 16 characters")
    thresholds = thresholds or {}
    if timeout <= 0 or not math.isfinite(timeout) or any(v < 0 or not math.isfinite(v) for v in thresholds.values()):
        raise ValueError("invalid timeout or calibration")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    app = FastAPI(title="CoRCA", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit)
    slots = threading.BoundedSemaphore(concurrency)
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorized(key: str | None = Depends(key_header)):
        if key is None or not secrets.compare_digest(key.encode(), token.encode()):
            raise HTTPException(401, "invalid API key")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz", dependencies=[Depends(authorized)])
    def ready():
        return {"status": "ok", "calibrated_methods": sorted(thresholds)}

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
    thresholds = {}
    if args.calibration:
        thresholds = calibration_thresholds(json.loads(Path(args.calibration).read_text()))
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(token, thresholds), host=args.host, port=args.port, timeout_keep_alive=5,
                limit_concurrency=8, timeout_graceful_shutdown=70)


if __name__ == "__main__":
    main()
