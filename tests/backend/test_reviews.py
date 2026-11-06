import json
import sqlite3

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from backend.api import Jobs, create_app
from backend.access import Access
from backend.reviews import OutcomeReview, Reviews

ACTOR={'id':'reviewer','name':'Reviewer'}
CONTEXT={'source':{'url':'http://private','credential':'DO_NOT_EXPOSE'},'thresholds':{'baro':6}}


def label(**changes):
    return dict(revision=0,outcome='fault',root_causes=['payments-db'],
                evidence_kind='controlled_drill',evidence='Fault injection record and recovery independently verified.',**changes)


def job(jobs, index, status='diagnosed', roots=None, method='baro', context=CONTEXT):
    row,_=jobs.add(method,1000+index,30,20,context=context)
    jobs.finish(row['id'],'failed' if status=='failed' else 'done',
                result=None if status=='failed' else {'status':status,'root_causes':roots if roots is not None else ['payments-db']})
    return row['id']


def test_scoring_faults_healthy_partial_unavailable_and_nulls(tmp_path):
    jobs=Jobs(tmp_path/'reviews.db');Access(jobs);reviews=Reviews(jobs)
    cases=[('diagnosed',['payments-db'],'fault'),('no_significant_anomaly',[],'fault'),
           ('partial_timeout',['payments-db'],'fault'),('failed',[],'fault'),
           ('insufficient_evidence',[],'fault'),('diagnosed',['wrong'],'healthy'),
           ('no_significant_anomaly',[],'healthy'),('failed',[],'healthy')]
    for i,(status,roots,outcome) in enumerate(cases):
        body=label();body.update(outcome=outcome,root_causes=['payments-db'] if outcome=='fault' else [])
        reviews.save(job(jobs,i,status,roots),OutcomeReview(**body),ACTOR)
    job(jobs,30)  # Unconfirmed diagnoses never enter metric denominators.
    result=reviews.summary();c=result['cohorts'][0]
    assert result['pending_count']==1 and c['cases']==8
    assert [c[k] for k in ('tp','fn','fp','tn','unavailable')]==[2,1,1,1,3]
    metrics={m['name']:m for m in c['metrics']}
    assert metrics['complete_exact_match']['value']==1/5
    assert metrics['mean_f1']['value']==2/5
    assert metrics['detection_recall']=={'name':'detection_recall','value':2/3,'samples':3}
    assert metrics['healthy_false_positive']['value']==0.5
    assert metrics['unavailable_rate']['value']==3/8
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)


def test_configuration_evidence_method_separation_duplicates_and_withdrawal(tmp_path):
    jobs=Jobs(tmp_path/'reviews.db');Access(jobs);reviews=Reviews(jobs)
    identifiers=[job(jobs,0),job(jobs,0),job(jobs,1,method='circa'),job(jobs,2,context={'thresholds':{'baro':9}}),job(jobs,3)]
    for i,identifier in enumerate(identifiers):
        body=label();body['evidence_kind']='synthetic' if i==4 else 'controlled_drill'
        reviews.save(identifier,OutcomeReview(**body),ACTOR)
    result=reviews.summary()
    assert len(result['cohorts'])==4 and result['duplicate_windows_excluded']==1
    assert sum(c['cases'] for c in result['cohorts'])==4
    body=label();body.update(revision=1,outcome='unconfirmed',root_causes=[])
    reviews.save(identifiers[-1],OutcomeReview(**body),ACTOR)
    result=reviews.summary()
    assert len(result['cohorts'])==3 and result['pending_count']==1
    assert result['recent'][0]['revision']==2
    with jobs._db() as db:
        event=json.loads(db.execute("SELECT details FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert event['previous']['outcome']=='fault' and event['review']['outcome']=='unconfirmed'


def test_pagination_validation_atomicity_and_restart(tmp_path):
    path=tmp_path/'reviews.db';jobs=Jobs(path);Access(jobs);reviews=Reviews(jobs)
    ids=[job(jobs,i) for i in range(22)]
    first=reviews.summary();second=reviews.summary(first['next_before'])
    assert len(first['pending'])==20 and len(second['pending'])==2
    assert not ({j['id'] for j in first['pending']} & {j['id'] for j in second['pending']})
    with TestClient(create_app(db_path=path)) as client:
        endpoint=f'/v1/diagnoses/{ids[0]}/review'
        assert client.get(endpoint).json()=={'review':None}
        assert client.post(endpoint,json=label()).status_code==200
        assert client.post(endpoint,json=label()).status_code==409
        invalid=label();invalid.update(outcome='healthy',evidence='SECRET_REJECTED')
        reply=client.post(endpoint,json=invalid)
        assert reply.status_code==422 and 'SECRET_REJECTED' not in reply.text
        queued,_=jobs.add('baro',9000,30,20)
        assert client.post(f"/v1/diagnoses/{queued['id']}/review",json=label()).status_code==409
        assert client.get('/v1/diagnoses/missing/review').status_code==404
    with TestClient(create_app(db_path=path)) as client:
        assert client.get(endpoint).json()['review']['revision']==1
    with jobs._db() as db:
        db.execute("CREATE TRIGGER reject_review_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        reviews.save(ids[1],OutcomeReview(**label()),ACTOR)
    assert reviews.get(ids[1]) is None
    for changes in ({'root_causes':[]},{'root_causes':['same','same']},{'evidence':' '*10},{'revision':True}):
        body=label();body.update(changes)
        with pytest.raises(ValidationError): OutcomeReview(**body)


def test_review_permissions_and_unknown_denominators(tmp_path):
    path=tmp_path/'reviews.db';jobs=Jobs(path);access=Access(jobs);reviews=Reviews(jobs)
    password='reviewer testing password'
    access.create_user('owner',password,'administrator',bootstrap=True)
    access.create_user('responder',password,'responder',ACTOR)
    identifier=job(jobs,0,status='failed')
    with TestClient(create_app('alert-service-testing-token',db_path=path,evaluation_token='evaluation-testing-token'),base_url='http://localhost') as client:
        endpoint=f'/v1/diagnoses/{identifier}/review'
        assert client.get('/v1/diagnosis-evaluations').status_code==401
        assert client.post(endpoint,json=label()).status_code==401
        for token in ('alert-service-testing-token','evaluation-testing-token'):
            assert client.post(endpoint,json=label(),headers={'X-API-Key':token}).status_code==403
        assert client.post('/v1/access/login',json={'username':'responder','password':password}).status_code==200
        assert client.post(endpoint,json=label(),headers={'Origin':'https://untrusted.example'}).status_code==403
        assert client.post(endpoint,json=label()).status_code==200
        summary=client.get('/v1/diagnosis-evaluations').json()
        metrics={m['name']:m for m in summary['cohorts'][0]['metrics']}
        assert metrics['detection_recall']['value'] is None
        assert metrics['healthy_false_positive']['samples']==0
        assert metrics['complete_exact_match']['value']==0
        assert metrics['unavailable_rate']['value']==1


def test_empty_partial_results_are_not_quiet_windows_and_names_match_exactly(tmp_path):
    jobs=Jobs(tmp_path/'reviews.db');Access(jobs);reviews=Reviews(jobs)
    for i,status in enumerate(('partial_timeout','partial_unexplained')):
        body=label();body.update(outcome='healthy',root_causes=[])
        reviews.save(job(jobs,i,status,[]),OutcomeReview(**body),ACTOR)
    body=label();body['root_causes']=['payments-db','redis-cart']
    reviews.save(job(jobs,3,roots=['payments-db','redis']),OutcomeReview(**body),ACTOR)
    c=reviews.summary()['cohorts'][0]
    metrics={m['name']:m for m in c['metrics']}
    assert c['unavailable']==2 and c['tn']==0
    assert metrics['healthy_false_positive']['value'] is None
    assert metrics['complete_exact_match']['value']==0
    assert metrics['mean_f1']['value']==0.5
