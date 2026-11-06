from datetime import datetime, timedelta, timezone
import sqlite3

from fastapi.testclient import TestClient

from backend.api import create_app
from backend.telemetry import validate_source

TOKEN = "alert-inbox-access-token"
AUTH = {"X-API-Key": TOKEN}


def source():
    return validate_source({"prometheus_url":"http://localhost:9090", "queries":{"cpu":"cpu"},
                            "services":["cart"], "edges":[], "step":5})


def hook(status="firing", identity="cart-latency", start=None):
    start = start or datetime.now(timezone.utc) + timedelta(minutes=5)
    return {"status":status, "groupKey":identity,
            "alerts":[{"status":status,"startsAt":start.isoformat(), "fingerprint":identity,
                       "labels":{"alertname":"CartLatency", "service":"cart", "severity":"critical"},
                       "annotations":{"summary":"Requests are timing out <script>unsafe()</script>", "description":"not retained"}}]}


def test_alerts_are_persisted_deduplicated_linked_and_resolved(tmp_path):
    database = tmp_path / "workspace.db"
    body = hook()
    with TestClient(create_app(TOKEN, {"baro":6}, source=source(), db_path=database)) as client:
        assert client.get("/v1/alerts").status_code == 401
        assert client.post("/v1/alerts/test").status_code == 401
        first = client.post("/v1/alerts", json=body, headers=AUTH)
        second = client.post("/v1/alerts", json=body, headers=AUTH)
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"] and second.json()["duplicate"]
        report = client.get("/v1/alerts", headers=AUTH).json()
        assert report["matching"] == 1 and report["delivery"]["firing"] == 1
        alert = report["alerts"][0]
        assert alert["job_id"] == first.json()["id"] and alert["diagnosis_status"] == "queued"
        assert alert["deliveries"] == 2 and "not retained" not in str(alert)
        resolved = {**body, "status":"resolved", "alerts":[{**body["alerts"][0], "status":"resolved", "endsAt":datetime.now(timezone.utc).isoformat()}]}
        assert client.post("/v1/alerts", json=resolved, headers=AUTH).json()["accepted"] is False
        assert client.get("/v1/alerts?status=firing", headers=AUTH).json()["matching"] == 0
        report = client.get("/v1/alerts?status=resolved&q=cart", headers=AUTH).json()
        assert report["matching"] == 1 and report["alerts"][0]["job_id"] == first.json()["id"]
        # A delayed retry cannot resurrect a resolved episode or enqueue more work.
        assert client.post("/v1/alerts", json=body, headers=AUTH).json()["accepted"] is False
        assert client.get("/v1/alerts?status=firing", headers=AUTH).json()["matching"] == 0
        assert len(client.get("/v1/diagnoses", headers=AUTH).json()) == 1
    with TestClient(create_app(TOKEN, db_path=database)) as client:
        report = client.get("/v1/alerts", headers=AUTH).json()
        assert report["alerts"][0]["status"] == "resolved"
        assert report["delivery"]["last_external_at"] is not None


def test_receiver_records_alerts_before_diagnosis_setup_and_local_test_is_distinct(tmp_path):
    database = tmp_path / "workspace.db"
    with TestClient(create_app(db_path=database)) as client:
        result = client.post("/v1/alerts", json=hook())
        assert result.status_code == 503
        report = client.get("/v1/alerts").json()
        assert not report["diagnoses_ready"] and report["receiver_ready"]
        assert report["delivery"]["firing"] == 1 and report["alerts"][0]["diagnosis_error"]
    with TestClient(create_app(db_path=tmp_path / "local-test.db")) as client:
        result = client.post("/v1/alerts/test").json()
        assert result["test_only"] and not result["external_delivery_verified"]
        report = client.get("/v1/alerts").json()
        assert report["delivery"]["last_external_at"] is None
        assert report["alerts"][0]["origin"] == "console_test" and report["alerts"][0]["job_id"] is None
        assert client.get("/v1/diagnoses").json() == []
        assert client.post("/v1/alerts/test", headers={"Origin":"https://untrusted.example"}).status_code == 403


def test_alertmanager_zero_end_is_not_a_firing_duration_and_resolution_keeps_job(tmp_path):
    database = tmp_path / 'workspace.db'
    body = hook()
    body['alerts'][0]['endsAt'] = '0001-01-01T00:00:00Z'
    with TestClient(create_app(thresholds={'baro':6},source=source(),db_path=database)) as client:
        first = client.post('/v1/alerts',json=body).json()
        assert client.get('/v1/alerts').json()['alerts'][0]['ends_at'] is None
        # Old workspaces may already contain Alertmanager's zero-time sentinel.
        with sqlite3.connect(database) as db:
            db.execute('UPDATE alert_events SET ends_at=-62135596800')
        assert client.get('/v1/alerts').json()['alerts'][0]['ends_at'] is None
        zero_resolution = {**body,'status':'resolved','alerts':[{**body['alerts'][0],'status':'resolved'}]}
        assert client.post('/v1/alerts',json=zero_resolution).json()['accepted'] is False
        assert client.get('/v1/alerts').json()['alerts'][0]['ends_at'] is None
        ended = datetime.fromisoformat(body['alerts'][0]['startsAt']) + timedelta(minutes=2)
        resolved = {**body,'status':'resolved','alerts':[{**body['alerts'][0],'status':'resolved','endsAt':ended.isoformat()}]}
        assert client.post('/v1/alerts',json=resolved).json()['accepted'] is False
        event = client.get('/v1/alerts').json()['alerts'][0]
        assert event['status']=='resolved' and event['ends_at']==ended.timestamp()
        assert event['job_id']==first['id']
        assert len(client.get('/v1/diagnoses').json())==1
        assert client.post('/v1/alerts',json=body).json()['accepted'] is False
        assert client.get('/v1/alerts').json()['alerts'][0]['ends_at']==ended.timestamp()


def test_queue_pressure_keeps_receipts_and_duplicate_job_links(tmp_path):
    with TestClient(create_app(thresholds={"baro":6}, source=source(), db_path=tmp_path / "workspace.db", max_pending=1)) as client:
        body = hook()
        first = client.post("/v1/alerts", json=body).json()
        rejected = client.post("/v1/alerts", json=hook(identity="other-alert"))
        assert rejected.status_code == 503
        report = client.get("/v1/alerts").json()
        assert report["matching"] == 2
        assert any("queue is full" in (event["diagnosis_error"] or "") for event in report["alerts"])
        repeated = client.post("/v1/alerts", json=body).json()
        assert repeated["duplicate"] and repeated["id"] == first["id"]


def test_server_side_filters_cover_events_beyond_the_latest_page(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "workspace.db")) as client:
        for i in range(205):
            assert client.post("/v1/alerts", json=hook(status="resolved", identity=f"event-{i}")).status_code == 200
        all_events = client.get("/v1/alerts").json()
        assert all_events["matching"] == 205 and len(all_events["alerts"]) == 200
        assert client.get("/v1/alerts?status=resolved&q=cart").json()["matching"] == 205
        assert client.get("/v1/alerts?status=firing").json()["matching"] == 0
        assert client.get("/v1/alerts?q=not-found").json()["matching"] == 0


def test_invalid_payloads_do_not_create_events(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "workspace.db")) as client:
        body = hook();body["alerts"][0]["labels"]["alertname"] = "x" * 513
        assert client.post("/v1/alerts", json=body).status_code == 422
        assert client.get("/v1/alerts").json()["matching"] == 0
        assert client.post("/v1/alerts", json=hook(start=datetime.now(timezone.utc)+timedelta(days=1))).status_code == 422
        assert client.get("/v1/alerts").json()["matching"] == 0


def grouped(*episodes):
    return {'status':'firing','groupKey':'shared-alert-route','alerts':list(episodes)}


def episode(identity,started,status='firing'):
    return {'status':status,'startsAt':started.isoformat(),'fingerprint':identity,
            'labels':{'alertname':identity,'service':'cart'}}


def test_late_joining_episode_keeps_original_link_and_gets_covering_diagnosis(tmp_path):
    start=datetime.now(timezone.utc)+timedelta(minutes=5)
    first_alert=episode('original',start);late_alert=episode('late',start+timedelta(minutes=25))
    with TestClient(create_app(thresholds={'baro':6},source=source(),db_path=tmp_path/'workspace.db',max_pending=2)) as client:
        first=client.post('/v1/alerts',json=grouped(first_alert)).json()
        later=client.post('/v1/alerts',json=grouped(first_alert,late_alert))
        assert later.status_code==200,later.text
        later=later.json()
        assert later['id']!=first['id'] and not later['duplicate']
        assert later['inject_time']<=datetime.fromisoformat(late_alert['startsAt']).timestamp()<=later['window'][1]
        rows={row['name']:row for row in client.get('/v1/alerts').json()['alerts']}
        assert rows['original']['job_id']==first['id'] and rows['late']['job_id']==later['id']
        assert {job['id'] for job in later['jobs']}=={first['id'],later['id']}
        assert all(set(job)=={'id','status','method','inject_time','window'} for job in later['jobs'])
        repeated=client.post('/v1/alerts',json=grouped(late_alert,first_alert)).json()
        assert repeated['duplicate'] and repeated['id']==later['id']
        assert {job['id'] for job in repeated['jobs']}=={first['id'],later['id']}
        assert len(client.get('/v1/diagnoses').json())==2
        # Later source/sensitivity changes must not remap already-linked episodes.
        revision=client.get('/v1/integrations').json()['revision']
        changed=source();changed['queries']={'cpu':'private_query_canary'}
        assert client.post('/v1/connections',json={'source':changed,'revision':revision}).status_code==200
        unchanged=client.post('/v1/alerts',json=grouped(first_alert,late_alert)).json()
        assert unchanged['duplicate'] and unchanged['id']==later['id']
        assert {job['id'] for job in unchanged['jobs']}=={first['id'],later['id']}
        assert 'private_query_canary' not in str(unchanged)


def test_initial_distant_alert_groups_split_but_simultaneous_alerts_share_jobs(tmp_path):
    start=datetime.now(timezone.utc)+timedelta(minutes=5)
    episodes=[episode('first',start),episode('near-first',start+timedelta(seconds=1)),
              episode('later',start+timedelta(minutes=25)),episode('near-later',start+timedelta(minutes=25,seconds=1))]
    with TestClient(create_app(thresholds={'baro':6},source=source(),db_path=tmp_path/'workspace.db')) as client:
        response=client.post('/v1/alerts',json=grouped(*episodes))
        assert response.status_code==200,response.text
        response=response.json();assert len(response['jobs'])==2
        rows={row['name']:row for row in client.get('/v1/alerts').json()['alerts']}
        assert rows['first']['job_id']==rows['near-first']['job_id']
        assert rows['later']['job_id']==rows['near-later']['job_id']
        assert rows['first']['job_id']!=rows['later']['job_id']
        for alert in episodes:
            job=client.get('/v1/diagnoses/'+rows[alert['labels']['alertname']]['job_id']).json()
            onset=datetime.fromisoformat(alert['startsAt']).timestamp()
            assert job['inject_time']<=onset<=job['window'][1]
        repeated=client.post('/v1/alerts',json=grouped(*reversed(episodes))).json()
        assert repeated['duplicate'] and repeated['id']==response['id']
        assert len(client.get('/v1/diagnoses').json())==2


def test_partial_group_saturation_retries_only_missing_work(tmp_path):
    start=datetime.now(timezone.utc)+timedelta(minutes=5)
    first_alert=episode('first',start);later_alert=episode('later',start+timedelta(minutes=25))
    body=grouped(first_alert,later_alert)
    with TestClient(create_app(thresholds={'baro':6},source=source(),db_path=tmp_path/'workspace.db',max_pending=1)) as client:
        assert client.post('/v1/alerts',json=body).status_code==503
        rows={row['name']:row for row in client.get('/v1/alerts').json()['alerts']}
        first_id=rows['first']['job_id']
        assert first_id and rows['later']['job_id'] is None and 'queue is full' in rows['later']['diagnosis_error']
        assert client.post('/v1/alerts',json=body).status_code==503
        assert len(client.get('/v1/diagnoses').json())==1
        retry_first=client.post('/v1/alerts',json=grouped(first_alert)).json()
        assert retry_first['id']==first_id and retry_first['duplicate']
        assert client.post('/v1/diagnoses/'+first_id+'/cancel').status_code==200
        complete=client.post('/v1/alerts',json=body)
        assert complete.status_code==200,complete.text
        complete=complete.json();assert not complete['duplicate'] and complete['id']!=first_id
        rows={row['name']:row for row in client.get('/v1/alerts').json()['alerts']}
        assert rows['first']['job_id']==first_id and rows['later']['job_id']==complete['id']
        assert rows['later']['diagnosis_error'] is None
        assert client.post('/v1/alerts',json=body).json()['duplicate']
        assert len(client.get('/v1/diagnoses').json())==2


def test_resolved_duplicate_payload_entries_and_stale_episodes_never_queue(tmp_path):
    start=datetime.now(timezone.utc)+timedelta(minutes=5)
    firing=episode('mixed',start);resolved=episode('mixed',start,'resolved')
    with TestClient(create_app(thresholds={'baro':6},source=source(),db_path=tmp_path/'workspace.db')) as client:
        assert client.post('/v1/alerts',json=grouped(firing,resolved)).json()['accepted'] is False
        assert client.get('/v1/diagnoses').json()==[]
        later=episode('new',start+timedelta(minutes=25))
        response=client.post('/v1/alerts',json=grouped(firing,later,later)).json()
        assert response['accepted'] and len(client.get('/v1/diagnoses').json())==1
        rows={row['name']:row for row in client.get('/v1/alerts').json()['alerts']}
        assert rows['mixed']['status']=='resolved' and rows['mixed']['job_id'] is None
        assert rows['new']['job_id']==response['id']


def test_long_alert_leads_cover_aligned_onset_without_exceeding_incident_samples(tmp_path):
    import math
    from backend.api import Incident,MAX_INCIDENT_ROWS
    start=datetime.now(timezone.utc)+timedelta(minutes=5)
    for step,lead in [(5,600),(60,3600)]:
        configured=source();configured.update(step=step,alert_lead_seconds=lead)
        with TestClient(create_app(thresholds={'baro':6},source=configured,db_path=tmp_path/f'step-{step}.db')) as client:
            response=client.post('/v1/alerts',json=grouped(episode('long-lead',start)))
            assert response.status_code==200,response.text
            job=response.json();onset=start.timestamp()
            assert job['inject_time']==onset-lead and job['window'][1]>=onset
            first=math.ceil(job['window'][0]/step);last=math.floor(job['window'][1]/step)
            times=[tick*step for tick in range(first,last+1)]
            assert len(times)<=MAX_INCIDENT_ROWS and sum(t>=onset for t in times)>=10
            Incident(columns=['time','cart_cpu'],rows=[[t,1] for t in times],inject_time=job['inject_time'])
            assert client.post('/v1/alerts',json=grouped(episode('long-lead',start))).json()['duplicate']


def test_excessive_alert_window_and_invalid_later_timestamp_are_not_queued(tmp_path):
    configured=source();configured.update(step=1,alert_lead_seconds=3600)
    start=datetime.now(timezone.utc)+timedelta(minutes=5)
    with TestClient(create_app(thresholds={'baro':6},source=configured,db_path=tmp_path/'oversize.db')) as client:
        response=client.post('/v1/alerts',json=grouped(episode('oversize',start)))
        assert response.status_code==422 and 'Reduce alert lead' in response.json()['detail']
        report=client.get('/v1/alerts').json()
        assert report['alerts'][0]['diagnosis_error']==response.json()['detail']
        assert client.get('/v1/diagnoses').json()==[]
    with TestClient(create_app(thresholds={'baro':6},source=source(),db_path=tmp_path/'future.db')) as client:
        response=client.post('/v1/alerts',json=grouped(episode('valid',start),episode('future',start+timedelta(days=1))))
        assert response.status_code==422
        assert client.get('/v1/alerts').json()['matching']==0 and client.get('/v1/diagnoses').json()==[]
