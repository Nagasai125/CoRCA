from datetime import datetime,timezone
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from runtime.api import create_app, Jobs
from runtime.access import Access
from runtime.quality import Evaluation, Evaluations, from_pilot, from_junit

TOKEN='pipeline-evaluation-test-token'
PASSWORD='administrator password for quality only'


def report(**changes):
    return {'run_key':'ci:123:1','kind':'diagnosis','name':'BARO held-out incidents','completed_at':datetime.now(timezone.utc).isoformat(),
            'source_revision':'abcdef1234','evidence_kind':'controlled_drill','scope':'Independent held-out cases; BARO, frozen threshold 6',
            'case_count':10,'metrics':[{'name':'complete_exact_match','value':0.8,'samples':5},{'name':'healthy_false_positive','value':0,'samples':5}],
            'gates':[], 'limitations':['Controlled fixtures, not customer incidents.'],**changes}


def test_publish_read_deduplicate_conflict_and_restart(tmp_path):
    db=tmp_path/'workspace.db';body=report()
    with TestClient(create_app(db_path=db,evaluation_token=TOKEN)) as client:
        assert client.get('/v1/evaluations').json()['runs']==[]
        first=client.post('/v1/evaluations',json=body,headers={'Authorization':'Bearer '+TOKEN})
        assert first.status_code==201 and not first.json()['duplicate']
        assert client.post('/v1/evaluations',json=body,headers={'Authorization':'Bearer '+TOKEN}).json()['duplicate']
        assert client.post('/v1/evaluations',json={**body,'name':'Changed evidence'},headers={'Authorization':'Bearer '+TOKEN}).status_code==409
        run=client.get('/v1/evaluations').json()['runs'][0]
        assert run['gate_status']=='not_assessed' and run['publisher']=='Evaluation pipeline'
        assert len(run['report_sha256'])==64
        assert client.get('/v1/evaluations?kind=help').json()['runs']==[]
        assert client.get('/v1/evaluations',headers={'Authorization':'Bearer '+TOKEN}).status_code==403
        assert client.get('/v1/config',headers={'Authorization':'Bearer '+TOKEN}).status_code==403
        assert client.post('/v1/alerts',json={'status':'resolved','groupKey':'x','alerts':[]},headers={'Authorization':'Bearer '+TOKEN}).status_code==403
    with TestClient(create_app(db_path=db)) as client:
        assert client.get('/v1/evaluations').json()['runs'][0]['run_key']==body['run_key']


def test_role_and_secret_boundaries(tmp_path):
    db=tmp_path/'workspace.db';access=Access(Jobs(db));access.create_user('owner',PASSWORD,'administrator',bootstrap=True)
    access.create_user('responder',PASSWORD,'responder',{'id':'fixture','name':'Fixture'})
    with TestClient(create_app('alertmanager-test-token',db_path=db,evaluation_token=TOKEN),base_url='http://localhost') as client:
        assert client.get('/v1/evaluations').status_code==401
        assert client.post('/v1/evaluations',json=report()).status_code==401
        assert client.post('/v1/evaluations',json=report(),headers={'X-API-Key':'alertmanager-test-token'}).status_code==403
        assert client.post('/v1/evaluations',json=report(),headers={'Authorization':'Bearer '+TOKEN}).status_code==201
        assert client.post('/v1/access/login',json={'username':'responder','password':PASSWORD}).status_code==200
        assert client.get('/v1/evaluations').status_code==200
        assert client.post('/v1/evaluations',json=report()).status_code==403
        client.post('/v1/access/logout')
        assert client.post('/v1/access/login',json={'username':'owner','password':PASSWORD}).status_code==200
        assert client.post('/v1/evaluations',json=report(run_key='manual:1')).status_code==201
        assert client.get('/v1/access/audit').json()['events'][0]['action']=='evaluation.published'
        assert client.post('/v1/evaluations',json=report(),headers={'Origin':'https://untrusted.example'}).status_code==403
        for body in [report(raw_logs='SECRET_INPUT'),report(pipeline_url='javascript:alert(1)'),report(pipeline_url='https://user:secret@example.com/run'),
                     report(metrics=[{'name':'complete_exact_match','value':1.1,'samples':10}]),
                     report(metrics=[{'name':'complete_exact_match','value':0.5,'samples':0}]),report(case_count=1),
                     report(completed_at='2099-01-01T00:00:00Z'),report(kind='release'),report(kind='help')]:
            rejected=client.post('/v1/evaluations',json=body)
            assert rejected.status_code==422 and 'SECRET_INPUT' not in rejected.text and 'user:secret' not in rejected.text
    with pytest.raises(ValueError,match='separate'):
        create_app(TOKEN,db_path=db,evaluation_token=TOKEN)


def test_gates_pagination_nulls_and_audit_failure_rolls_back(tmp_path):
    jobs=Jobs(tmp_path/'workspace.db');access=Access(jobs);store=Evaluations(jobs);actor={'id':'operator','name':'Operator'}
    for key,status in [('one','pass'),('two','fail'),('three','not_run')]:
        store.publish(Evaluation(**report(run_key=key,gates=[{'name':'Exact match','status':status,'criterion':'At least 80% exact match on five independent faults'}])),actor)
    page=store.recent(limit=2)
    assert [r['gate_status'] for r in page['runs']]==['incomplete','failed']
    assert store.recent(before=page['next_before'],limit=2)['runs'][0]['gate_status']=='passed'
    store.publish(Evaluation(**report(run_key='zero',case_count=0,metrics=[{'name':'mean_f1','value':None,'samples':0}])),actor)
    assert store.recent()['runs'][0]['metrics'][0]['value'] is None
    with jobs._db() as db:
        db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.publish(Evaluation(**report(run_key='rolled-back')),actor)
    assert all(r['run_key']!='rolled-back' for r in store.recent()['runs'])


def test_existing_pilot_and_junit_reports_are_summarized_without_raw_cases(tmp_path):
    metadata={k:v for k,v in report().items() if k in ('run_key','name','scope','completed_at','source_revision')}
    raw={'kind':'pilot_case_assessment','evidence_kind':'real_incident','cases':[{'incident_id':'PRIVATE_INCIDENT'}],
         'by_method':{'baro':{'cases':10,'fault_cases':5,'healthy_cases':5,'evaluable_healthy_cases':4,'complete_fault_exact_match_rate':.8,
                              'healthy_false_positive_rate':.25,'fault_mean_F1':.9,'unavailable':1}}}
    prepared=from_pilot(raw,'baro',**metadata)
    assert 'PRIVATE_INCIDENT' not in prepared.model_dump_json()
    assert prepared.metrics[1].samples==4 and prepared.metrics[-1].value==.1 and not prepared.gates
    path=tmp_path/'junit.xml';path.write_text('<testsuites><testsuite><testcase name="private-test"/><testcase><skipped/></testcase><testcase><failure/></testcase></testsuite></testsuites>')
    prepared=from_junit(path,**metadata)
    assert prepared.case_count==3 and prepared.gates[0].status=='fail' and prepared.metrics[0].value==pytest.approx(1/3)
    assert 'private-test' not in prepared.model_dump_json()


def test_cli_prepares_private_summary_and_publishes_with_scoped_token(tmp_path,monkeypatch):
    from runtime import quality
    path=tmp_path/'tests.xml';path.write_text('<testsuite><testcase/><testcase/></testsuite>')
    out=tmp_path/'summary.json'
    args=['quality','junit','--input',str(path),'--out',str(out),'--run-key','ci:1','--name','Software checks','--scope','Two local software cases only']
    monkeypatch.setattr('sys.argv',args);quality.main()
    assert out.stat().st_mode & 0o077==0
    assert Evaluation.model_validate_json(out.read_text()).case_count==2
    with pytest.raises(SystemExit): quality.main() # never overwrite published evidence
    calls=[]
    import requests
    def post(url,**kwargs):
        calls.append((url,kwargs))
        return type('Reply',(),{'status_code':201})()
    monkeypatch.setattr(requests,'post',post)
    monkeypatch.setenv('CORCA_EVALUATION_TOKEN',TOKEN)
    monkeypatch.setattr('sys.argv',['quality','publish','--report',str(out),'--url','https://corca.example.com'])
    quality.main()
    assert calls[0][0]=='https://corca.example.com/v1/evaluations'
    assert calls[0][1]['headers']['Authorization']=='Bearer '+TOKEN
    assert calls[0][1]['allow_redirects'] is False
    monkeypatch.setattr('sys.argv',['quality','publish','--report',str(out),'--url','http://remote.example.com'])
    with pytest.raises(SystemExit):quality.main()
    assert len(calls)==1
