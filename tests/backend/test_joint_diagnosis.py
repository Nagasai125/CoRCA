import copy
import json
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import api
from backend.api import Jobs, create_app
from backend.fusion import fuse
from backend.reviews import OutcomeReview, Reviews
from backend.uncertainty import estimate
from corca.structural import repair_effects


def exports():
    times = np.arange(1000, 1400, 5)
    frame = pd.DataFrame({"time": times, "~frontend_cpu": np.linspace(1, 2, len(times)),
                          "~db_cpu": np.where(times < 1300, 1., 20.)})
    spans, logs = {"frontend": [], "db": []}, []
    for i, t in enumerate(times):
        trace, parent, child = f"{i+1:032x}", f"{2*i+1:016x}", f"{2*i+2:016x}"
        for service, identifier, parent_id, kind in [("frontend",parent,"",3),("db",child,parent,2)]:
            spans[service].append({"traceId":trace,"spanId":identifier,"parentSpanId":parent_id,
                "kind":kind,"startTimeUnixNano":str(int(t)*10**9),"endTimeUnixNano":str(int(t)*10**9+10**7),
                "status":{"code":2 if t>=1300 else 1}})
        logs.append({"timeUnixNano":str(int(t)*10**9),"severityNumber":17 if t>=1300 else 9,
                     "traceId":trace,"spanId":child,"body":{"stringValue":"PRIVATE LOG BODY"}})
    resource=lambda name:{"attributes":[{"key":"service.name","value":{"stringValue":name}}]}
    traces={"resourceSpans":[{"resource":resource(s),"scopeSpans":[{"spans":v}]} for s,v in spans.items()]}
    log_export={"resourceLogs":[{"resource":resource("db"),"scopeLogs":[{"logRecords":logs}]}]}
    return frame,traces,log_export


def bundle():
    frame,traces,logs=exports()
    return {"environment":"staging","incident":{"columns":list(frame.columns),"rows":frame.values.tolist(),
        "inject_time":1300,"tau":6},"traces":traces,"logs":logs}


def test_fusion_aligns_three_modalities_and_uses_actual_parent_links():
    frame,traces,logs=exports()
    combined,meta=fuse(frame,1300,traces,logs)
    assert meta["topology"]["edges"]==[("frontend","db")]
    assert set(meta["modalities"].values())=={"metrics","traces","logs"}
    assert meta["linked_log_count"]==80
    assert combined.loc[60,"~db_log-error"]==1
    assert combined.loc[0,"~db_trace-error"]==0
    assert combined.loc[60,"~db_trace-error"]==1
    assert "PRIVATE LOG BODY" not in json.dumps(meta)
    # A missing bucket stays unknown, not zero evidence.
    missing=copy.deepcopy(logs)
    missing["resourceLogs"][0]["scopeLogs"][0]["logRecords"].pop(1)
    result,_=fuse(frame,1300,traces,missing)
    assert np.isnan(result.loc[1,"~db_log-error"])


def test_topology_changes_are_observed_edges_not_guessed_from_names():
    frame,traces,logs=exports()
    db=traces["resourceSpans"][1]["scopeSpans"][0]["spans"]
    for span in db[:60]:span["parentSpanId"]=""
    _,meta=fuse(frame,1300,traces,logs)
    assert meta["topology"]["added"]==[("frontend","db")]
    for span in db:span["parentSpanId"]="f"*16
    _,meta=fuse(frame,1300,traces)
    assert meta["topology"]["edges"]==[]
    assert "missing parents" in meta["notes"][0]


@pytest.mark.parametrize("change",["timestamp","identifier","duplicate","severity"])
def test_import_rejects_invalid_telemetry(change):
    body=bundle()
    spans=body["traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    if change=="timestamp":spans[0]["startTimeUnixNano"]="nan"
    if change=="identifier":spans[0]["traceId"]="not-a-trace"
    if change=="duplicate":spans.append({**spans[0],"endTimeUnixNano":"2000000000000"})
    if change=="severity":body["logs"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["severityNumber"]=True
    frame=pd.DataFrame(body["incident"]["rows"],columns=body["incident"]["columns"])
    with pytest.raises(ValueError):fuse(frame,1300,body["traces"],body["logs"])


def test_import_queue_preserves_derived_inputs_and_never_raw_logs(tmp_path,monkeypatch):
    db=tmp_path/"workspace.db"
    # Execute synchronously below so this test does not race the background worker.
    client=TestClient(create_app("joint-test-token-123",db_path=db))
    response=client.post("/v1/diagnoses/import",json=bundle())
    assert response.status_code==401
    client.headers["X-API-Key"]="joint-test-token-123"
    response=client.post("/v1/diagnoses/import",json=bundle())
    assert response.status_code==202,response.text
    identifier=response.json()["id"]
    jobs=Jobs(db);job=jobs.claim()
    assert job["id"]==identifier
    assert "PRIVATE LOG BODY" not in job["context"] and "resourceSpans" not in job["context"]
    def diagnosis(incident,thresholds,timeout,**kwargs):
        assert "~db_log-error" in incident.columns and "~db_trace-error" in incident.columns
        assert incident.edges==[("frontend","db")]
        return {"status":"diagnosed","root_causes":["db"],"rounds":[{"service":"db","confidence":.8}]}
    monkeypatch.setattr(api,"diagnose",diagnosis)
    api.run_job(job,jobs,None,{},10,lambda *a:pytest.fail("Uploaded jobs must not query monitoring"))
    result=jobs.get(identifier)
    assert result["status"]=="done"
    assert result["result"]["fusion"]["used_modalities"]==["logs","metrics","traces"]
    evidence=client.get(f"/v1/diagnoses/{identifier}/evidence").json()
    assert {s.get("modality") for s in evidence["series"]}=={"metrics","logs","traces"}
    assert any(s["service"]=="db" and s["metric"]=="trace-error" for s in evidence["series"])
    assert client.get(f"/v1/diagnoses/{identifier}/uncertainty").json()["available"] is False
    repeated=client.post(f"/v1/diagnoses/{identifier}/rerun")
    assert repeated.status_code==202 and repeated.json()["id"]!=identifier
    with jobs._db() as connection:
        new=json.loads(connection.execute("SELECT context FROM jobs WHERE id=?",(repeated.json()["id"],)).fetchone()[0])
    assert new==json.loads(job["context"])


def test_import_requires_separate_sensitivity_and_explicit_topology(tmp_path):
    client=TestClient(create_app(db_path=tmp_path/"workspace.db"))
    data=bundle();data["incident"].pop("tau")
    assert client.post("/v1/diagnoses/import",json=data).status_code==422
    data=bundle();data.pop("traces")
    assert client.post("/v1/diagnoses/import",json=data).status_code==422
    data["incident"]["edges"]=[]
    assert client.post("/v1/diagnoses/import",json=data).status_code==202


def test_real_localizer_uses_trace_evidence_when_resource_metrics_are_quiet(tmp_path):
    pytest.importorskip("RCAEval")
    data=bundle()
    for row in data["incident"]["rows"]:
        row[1:]=[1.,1.]
    bare=api.Incident.model_validate({**data["incident"],"edges":[],"anomaly_threshold":6})
    assert api.diagnose(bare,{},30)["status"]=="no_significant_anomaly"
    jobs=Jobs(tmp_path/"workspace.db")
    client=TestClient(create_app(db_path=jobs.path))
    response=client.post("/v1/diagnoses/import",json=data)
    assert response.status_code==202,response.text
    api.run_job(jobs.claim(),jobs,None,{},30,lambda *a:pytest.fail("No live metrics required"))
    job=jobs.get(response.json()["id"])
    assert job["status"]=="done",job
    assert "db" in job["result"]["root_causes"]
    assert "traces" in job["result"]["fusion"]["used_modalities"]


def structural_frame():
    rng=np.random.default_rng(42)
    db=rng.normal(10,1,240)
    front=2*db+rng.normal(0,.05,240)
    db[200:]+=5
    front[200:]+=13  # 10 propagated + 3 independent frontend disturbance.
    return pd.DataFrame({"time":np.arange(240),"db_latency-90":db,"frontend_latency-90":front})


def test_structural_repair_preserves_an_independent_caller_disturbance():
    frame=structural_frame()
    report=repair_effects(frame,200,[("frontend","db")],["db"])
    assert report["available"],report
    effects={v["service"]:v for v in report["repairs"][0]["effects"]}
    assert effects["frontend"]["estimated_reduction"]==pytest.approx(10,abs=.8)
    assert effects["frontend"]["after_repair"]>22
    assert report["kind"]=="model_based_intervention"
    assert len(report["validation"])==1


def test_structural_model_abstains_for_cycles_or_unidentifiable_baselines():
    frame=structural_frame()
    assert not repair_effects(frame,200,[("frontend","db"),("db","frontend")],["db"])["available"]
    frame.loc[:199,"db_latency-90"]=10
    assert not repair_effects(frame,200,[("frontend","db")],["db"])["available"]


def reviewed_jobs(tmp_path):
    jobs=Jobs(tmp_path/"workspace.db");api.Access(jobs);reviews=Reviews(jobs)
    context={"analysis_profile":"environment-policy-a","source":{"provider":"uploaded"}}
    actor={"id":"test","name":"reviewer","role":"administrator"}
    for i in range(40):
        job,_=jobs.add("baro",1000+i*120,30,10,context=context)
        jobs.finish(job["id"],"done",result={"status":"diagnosed","root_causes":["db"],
            "rounds":[{"service":"db","confidence":.9}]})
        outcome=OutcomeReview(revision=0,outcome="fault",root_causes=["db"] if i%2 else ["cache"],
            evidence_kind="controlled_drill",evidence="Independent fault injection record verified.")
        reviews.save(job["id"],outcome,actor)
    target,_=jobs.add("baro",10000,30,10,context=context)
    jobs.finish(target["id"],"done",result={"status":"diagnosed","root_causes":["db"],
        "rounds":[{"service":"db","confidence":.9}]})
    return jobs,target


def test_probabilities_use_disjoint_prior_confirmed_incidents(tmp_path):
    jobs,target=reviewed_jobs(tmp_path)
    result=estimate(jobs,target["id"])
    assert result["available"],result
    assert result["eligible_incidents"]==40
    assert .3<result["candidates"][0]["probability"]<.7
    assert result["validation"]["incidents"]==12
    assert result["validation"]["brier_score"]<result["validation"]["uncalibrated_brier_score"]
    # Later outcomes and changed configurations cannot leak into calibration.
    with jobs._db() as db:
        db.execute("UPDATE jobs SET context=? WHERE id=?",(json.dumps({"analysis_profile":"other"}),target["id"]))
    assert estimate(jobs,target["id"])["eligible_incidents"]==0


def test_synthetic_outcomes_do_not_unlock_calibrated_probabilities(tmp_path):
    jobs,target=reviewed_jobs(tmp_path)
    with jobs._db() as db:
        rows=db.execute("SELECT job_id,payload FROM diagnosis_reviews").fetchall()
        for row in rows:
            value=json.loads(row["payload"]);value["evidence_kind"]="synthetic"
            db.execute("UPDATE diagnosis_reviews SET payload=? WHERE job_id=?",(json.dumps(value),row["job_id"]))
    assert estimate(jobs,target["id"])["available"] is False


def test_repeated_windows_and_later_incidents_do_not_inflate_calibration(tmp_path):
    jobs,target=reviewed_jobs(tmp_path)
    with jobs._db() as db:
        context=json.loads(db.execute("SELECT context FROM jobs WHERE id=?",(target["id"],)).fetchone()[0])
        review=dict(db.execute("SELECT * FROM diagnosis_reviews LIMIT 1").fetchone())
    for onset in (1000,1000,10000,20000):
        job,_=jobs.add("baro",onset,30,10,context=context)
        jobs.finish(job["id"],"done",result={"root_causes":["db"],"rounds":[{"service":"db","confidence":.9}]})
        with jobs._db() as db:
            db.execute("INSERT INTO diagnosis_reviews VALUES (?,?,?,?,?)",(job["id"],review["revision"],review["payload"],review["reviewer"],review["reviewed_at"]))
    assert estimate(jobs,target["id"])["eligible_incidents"]==40

