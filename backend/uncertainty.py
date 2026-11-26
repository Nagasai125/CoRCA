"""Estimate candidate correctness from independent, confirmed earlier incidents."""
import hashlib
import json

import numpy as np
from sklearn.linear_model import LogisticRegression

POLICY = "reviewed-candidate-sigmoid-v1"


def profile(job):
    context = json.loads(job["context"] or "{}")
    configuration = context.get("analysis_profile") or context
    return hashlib.sha256(json.dumps([job["method"], configuration,
        job["inject_time"] - job["start"], job["end"] - job["inject_time"]],
        sort_keys=True).encode()).hexdigest()


def estimate(jobs, identifier):
    with jobs._db() as db:
        target = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if target is None:
            raise KeyError(identifier)
        rows = db.execute("""SELECT j.*, r.payload FROM jobs j JOIN diagnosis_reviews r ON j.id=r.job_id
            WHERE j.status='done' AND j.end < ? ORDER BY j.end DESC, j.id LIMIT 2000""", (target["start"],)).fetchall()
    key, selected, next_start = profile(target), [], float("inf")
    for job in rows:
        review = json.loads(job["payload"])
        if (profile(job) != key or review["outcome"] == "unconfirmed"
                or review["evidence_kind"] == "synthetic" or job["end"] >= next_start):
            continue
        result = json.loads(job["result"] or "{}")
        rounds = result.get("rounds", [])
        if not rounds:
            continue
        samples = [(r.get("confidence"), int(r["service"] in review["root_causes"])) for r in rounds]
        if any(not isinstance(s, (int, float)) or not np.isfinite(s) or not 0 <= s <= 1 for s, _ in samples):
            continue
        selected.append((job["id"], samples))
        next_start = job["start"]
    base = {"policy": POLICY, "available": False, "eligible_incidents": len(selected),
            "required_incidents": 30, "scope": "Correctness of selected candidates, not completeness of the cause set"}
    if len(selected) < 30:
        return {**base, "reason": "Review at least 30 earlier, nonoverlapping incidents with matching telemetry and settings; synthetic outcomes are excluded."}
    selected.reverse()
    split = max(20, int(len(selected) * .7))
    train, validation = selected[:split], selected[split:]
    def flatten(groups):
        xs, ys, weights = [], [], []
        for _, samples in groups:
            for score, label in samples:
                xs.append([score]); ys.append(label); weights.append(1 / len(samples))
        return np.array(xs), np.array(ys), np.array(weights)
    x, y, weights = flatten(train)
    vx, vy, vw = flatten(validation)
    if min(int((y == 0).sum()), int((y == 1).sum())) < 5 or len(set(vy)) < 2:
        return {**base, "reason": "Calibration needs both correct and incorrect candidates in separate training and validation incidents."}
    model = LogisticRegression(C=1., solver="lbfgs", random_state=0).fit(x, y, sample_weight=weights)
    probabilities = model.predict_proba(vx)[:, 1]
    brier = float(np.average((probabilities - vy) ** 2, weights=vw))
    original = float(np.average((vx[:, 0] - vy) ** 2, weights=vw))
    validation_info = {"incidents": len(validation), "candidates": len(vy), "brier_score": brier,
                       "uncalibrated_brier_score": original, "training_incidents": len(train)}
    if brier > original:
        return {**base, "reason": "The fitted calibration did not improve held-out Brier score; probability estimates are withheld.",
                "validation": validation_info}
    result = json.loads(target["result"] or "{}")
    candidates = []
    for record in result.get("rounds", []):
        score = record.get("confidence")
        if not isinstance(score, (float, int)) or not np.isfinite(score) or not x.min() <= score <= x.max():
            candidates.append({"service": record["service"], "probability": None, "reason": "Outside observed calibration score range"})
            continue
        probability = float(model.predict_proba([[score]])[0, 1])
        candidates.append({"service": record["service"], "probability": probability})
    fingerprint = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
    return {**base, "available": True, "candidates": candidates, "validation": validation_info,
            "training_fingerprint": fingerprint, "note": "Estimated from confirmed earlier incidents; environment drift can invalidate calibration."}

