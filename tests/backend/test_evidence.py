import json
import time

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from backend.api import Jobs, create_app, run_job
from backend.evidence import capture_evidence


def test_capture_is_bounded_preserves_exact_names_gaps_and_no_credentials():
    frame=pd.DataFrame({'time':np.arange(2500,dtype=float)+1000,'~payments-db_cpu':np.arange(2500,dtype=float),'~payments-db_mem':np.ones(2500),'~redis-cart_cpu':np.ones(2500)})
    frame.loc[1210:1250,'~payments-db_cpu']=np.nan
    evidence=capture_evidence(frame,{'edges':[['payments-db','redis-cart']],'token':'SECRET'},2200)
    assert evidence['samples']==2500 and evidence['preview_points']<=120
    assert [s['service'] for s in evidence['series']]==['payments-db','redis-cart']
    assert evidence['series'][0]['gaps']==[[2210.,2250.]]
    assert any(p[0]==2200 for p in evidence['series'][0]['points'])
    assert 'SECRET' not in json.dumps(evidence,allow_nan=False)


def test_saved_evidence_is_separate_from_polled_jobs_private_and_restart_safe(tmp_path,monkeypatch):
    from backend import api
    path=tmp_path/'workspace.db';jobs=Jobs(path)
    source={'edges':[['frontend','db']],'services':['frontend','db','isolated-missing'],'credential':'SECRET'}
    row,_=jobs.add('baro',time.time()-1000,300,120,context={'source':source,'thresholds':{'baro':6}})
    claim=jobs.claim()
    frame=pd.DataFrame({'time':np.arange(row['window'][0],row['window'][1]+1,5),'~frontend_cpu':1.})
    monkeypatch.setattr(api,'diagnose',lambda *args:{'status':'no_significant_anomaly','root_causes':[]})
    run_job(claim,jobs,source,{'baro':6},20,lambda *args:frame)
    job=jobs.get(row['id']);assert job['status']=='done'
    assert job['result']['processing_seconds']>=0 and job['result']['time_to_result_seconds']>=0
    assert 'series' not in json.dumps(job)
    token='evidence-only-test-token'
    with TestClient(create_app(token,db_path=path)) as client:
        endpoint=f"/v1/diagnoses/{row['id']}/evidence"
        assert client.get(endpoint).status_code==401
        result=client.get(endpoint,headers={'X-API-Key':token})
        assert result.status_code==200 and result.json()['available']
        assert result.json()['scope']==['frontend','db','isolated-missing']
        assert [series['service'] for series in result.json()['series']]==['frontend']
        assert 'SECRET' not in result.text
        assert client.get('/assets/workspace-visuals.js').status_code==200
        styles=client.get('/assets/workspace-visuals.css')
        assert styles.status_code==200 and 'text/css' in styles.headers['content-type']
    with TestClient(create_app(db_path=path)) as client:
        assert client.get(endpoint).json()['series'][0]['service']=='frontend'
        legacy,_=jobs.add('baro',time.time()+1000,300,120,context={'source':source})
        old=client.get(f"/v1/diagnoses/{legacy['id']}/evidence").json()
        assert old['available'] is False and old['edges']==[['frontend','db']]
        assert 'SECRET' not in json.dumps(old)
        assert client.get('/v1/diagnoses/missing/evidence').status_code==404
