"""Concurrent admission, alert retries, and atomic work claims on isolated SQLite."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import socket
import subprocess
import sys
import time

import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api import Jobs, create_app
from backend.telemetry import validate_source


def test_running_server_handles_ten_concurrent_readers(tmp_path):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    env={key:value for key,value in os.environ.items() if not key.startswith('CORCA_')}
    env.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
    process=subprocess.Popen([sys.executable,'-m','backend.api','--port',str(port),'--db',str(tmp_path/'workspace.db')],
                             env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    url=f'http://127.0.0.1:{port}/readyz'
    try:
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            assert process.poll() is None,'Application exited during startup'
            try:
                if requests.get(url,timeout=1).status_code==200:break
            except requests.RequestException:pass
            time.sleep(.05)
        else:raise AssertionError('Application did not become ready')
        with ThreadPoolExecutor(max_workers=10) as pool:
            codes=list(pool.map(lambda _:requests.get(url,timeout=5).status_code,range(100)))
        assert codes==[200]*100,'Ordinary readers must not exhaust HTTP capacity'
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()


def test_concurrent_queue_admission_stays_bounded_and_recovers(tmp_path):
    jobs = Jobs(tmp_path / 'workspace.db')

    def submit(_):
        try:
            return jobs.add('baro', time.time() + 1800, 300, 120, max_pending=8)[0]
        except HTTPException as error:
            assert error.status_code == 503 and error.headers['Retry-After'] == '30'
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(submit, range(40)))
    accepted = [job for job in responses if job]
    assert len(accepted) == 8
    assert len(jobs.recent(100)) == 8
    assert jobs.cancel(accepted[0]['id'])
    replacement = submit(None)
    assert replacement and replacement['status'] == 'queued'
    assert sum(job['status'] == 'queued' for job in jobs.recent(100)) == 8


def test_concurrent_alert_retries_share_one_job_and_cannot_reopen_resolution(tmp_path):
    source = validate_source({'prometheus_url':'http://localhost:9090', 'queries':{'cpu':'cpu'},
                              'services':['cart'], 'edges':[], 'step':5})
    started = datetime.now(timezone.utc) + timedelta(minutes=5)
    firing = {'status':'firing','groupKey':'concurrent-alert', 'alerts':[
        {'status':'firing','startsAt':started.isoformat(),'fingerprint':'same-episode',
         'labels':{'alertname':'HighCPU','service':'cart'}}]}
    resolved = {**firing, 'status':'resolved', 'alerts':[{**firing['alerts'][0], 'status':'resolved',
                 'endsAt':(started + timedelta(minutes=1)).isoformat()}]}
    with TestClient(create_app(thresholds={'baro':6}, source=source,
                               db_path=tmp_path/'workspace.db',max_pending=1)) as client:
        def deliver(body):
            reply = client.post('/v1/alerts',json=body)
            assert reply.status_code == 200, reply.text
            return reply.json()

        with ThreadPoolExecutor(max_workers=12) as pool:
            replies = list(pool.map(deliver,[firing]*24))
        assert len({reply['id'] for reply in replies}) == 1
        assert sum(not reply['duplicate'] for reply in replies) == 1
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(deliver,[resolved,firing]*12))
        report = client.get('/v1/alerts').json()
        assert report['matching'] == 1
        assert report['alerts'][0]['deliveries'] == 48
        assert report['alerts'][0]['status'] == 'resolved'
        assert len(client.get('/v1/diagnoses').json()) == 1


def test_concurrent_claims_do_not_execute_the_same_job_twice(tmp_path):
    jobs = Jobs(tmp_path/'workspace.db')
    expected = {jobs.add('baro',time.time()-1000,300,120)[0]['id'] for _ in range(40)}
    with ThreadPoolExecutor(max_workers=16) as pool:
        claimed = list(pool.map(lambda _: jobs.claim(),range(60)))
    actual = [job['id'] for job in claimed if job]
    assert len(actual) == len(set(actual)) == 40
    assert set(actual) == expected
    assert all(job['status'] == 'running' and job['attempts'] == 1 for job in jobs.recent(100))
