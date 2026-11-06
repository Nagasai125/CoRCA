from datetime import datetime,timezone
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api import create_app, Jobs
from backend.access import Access
from backend.quality import Evaluation, Evaluations, from_pilot, from_junit

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
    from backend import quality
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


def test_per_incident_reports_require_complete_unique_labels_and_keep_legacy_idempotency(tmp_path):
    case={'case_id':'case-1','fault_type':'DB exhaustion','outcome':'fault','truth':['payments-db'],
          'predicted':['payments-db'],'status':'diagnosed','seconds':141}
    body=report(case_count=1,metrics=[],dataset_id='incidents-v1',cases=[case])
    with TestClient(create_app(db_path=tmp_path/'workspace.db')) as client:
        assert client.post('/v1/evaluations',json=body).status_code==201
        published=client.get('/v1/evaluations').json()['runs'][0]
        assert published['cases'][0]['truth']==['payments-db']
        for changes in ({'dataset_id':None},{'case_count':2},{'cases':[case,case],'case_count':2},
                        {'cases':[{**case,'truth':[]}]},{'cases':[{**case,'seconds':-1}]},
                        {'cases':[{**case,'status':'no_significant_anomaly'}]},
                        {'kind':'release','evidence_kind':'software_test'}):
            assert client.post('/v1/evaluations',json={**body,'run_key':'bad',**changes}).status_code==422
    jobs=Jobs(tmp_path/'workspace.db');store=Evaluations(jobs)
    legacy=report(run_key='legacy-shape');stored=Evaluation(**legacy).model_dump(mode='json')
    stored.pop('cases');stored.pop('dataset_id')
    import hashlib
    payload=json.dumps(stored,sort_keys=True,separators=(',',':'))
    with jobs._db() as db:
        db.execute('INSERT INTO evaluation_runs (run_key,kind,completed_at,payload,report_sha256,publisher_id,publisher_name,received_at) VALUES (?,?,?,?,?,?,?,?)',
                   ('legacy-shape','diagnosis',1,payload,hashlib.sha256(payload.encode()).hexdigest(),'legacy','Legacy',1))
    assert store.publish(Evaluation(**legacy),{'id':'owner','name':'Owner'})['duplicate'] is True
    with jobs._db() as db:
        assert db.execute("SELECT payload FROM evaluation_runs WHERE run_key='legacy-shape'").fetchone()[0]==payload


def test_cli_malformed_junit_has_safe_error_and_no_output(tmp_path,monkeypatch,capsys):
    from backend import quality
    path=tmp_path/'broken.xml';path.write_text('<testsuite><testcase>PRIVATE_INPUT')
    out=tmp_path/'summary.json'
    monkeypatch.setattr('sys.argv',['quality','junit','--input',str(path),'--out',str(out),'--run-key','bad:1','--name','Checks','--scope','Local checks'])
    with pytest.raises(SystemExit) as error:quality.main()
    assert error.value.code==1 and not out.exists()
    message=capsys.readouterr().err
    assert 'Check inputs' in message and 'PRIVATE_INPUT' not in message and 'Traceback' not in message


@pytest.mark.parametrize('status,expected',[(401,'publisher token'),(403,'separate evaluation'),(409,'new run key'),(422,'schema'),(503,'Retry the same report')])
def test_cli_publish_errors_explain_next_step_without_echoing_server_data(tmp_path,monkeypatch,capsys,status,expected):
    from backend import quality
    import requests
    path=tmp_path/'summary.json';path.write_text(Evaluation(**report()).model_dump_json())
    monkeypatch.setenv('CORCA_EVALUATION_TOKEN',TOKEN)
    monkeypatch.setattr('sys.argv',['quality','publish','--report',str(path),'--url','https://corca.example.com'])
    monkeypatch.setattr(requests,'post',lambda *args,**kwargs:type('Reply',(),{'status_code':status,'text':'SECRET_SERVER_DATA'})())
    with pytest.raises(SystemExit) as error:quality.main()
    assert error.value.code==1
    message=capsys.readouterr().err
    assert f'HTTP {status}' in message and expected in message and 'SECRET_SERVER_DATA' not in message


def test_per_case_summary_metrics_cannot_contradict_evidence():
    cases=[{'case_id':'fault','fault_type':'DB','outcome':'fault','truth':['db'],'predicted':['db'],'status':'failed'},
           {'case_id':'healthy','fault_type':'Healthy','outcome':'healthy','truth':[],'predicted':[],'status':'partial_timeout'}]
    body=report(case_count=2,dataset_id='cases-v1',cases=cases,metrics=[{'name':'complete_exact_match','value':0,'samples':1},
        {'name':'mean_f1','value':0,'samples':1},{'name':'healthy_false_positive','value':None,'samples':0},
        {'name':'unavailable_rate','value':1,'samples':2}])
    assert Evaluation(**body).metrics[0].value==0
    for change in ({'name':'complete_exact_match','value':1,'samples':1},{'name':'mean_f1','value':1,'samples':1},
                   {'name':'healthy_false_positive','value':0,'samples':1},{'name':'unavailable_rate','value':0,'samples':2}):
        with pytest.raises(ValueError,match='per-case evidence'):Evaluation(**{**body,'metrics':[change]})
    partial={**cases[0],'status':'partial_unexplained'}
    valid={**body,'cases':[partial,cases[1]],'metrics':[{'name':'complete_exact_match','value':0,'samples':1},{'name':'mean_f1','value':1,'samples':1}]}
    assert Evaluation(**valid).metrics[1].value==1
