"""Identity boundaries, revocation, secret redaction, and atomic configuration auditing."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from runtime.access import Access, COOKIE
from runtime.api import Jobs, create_app

PASSWORD = "a long owner password 123!"
RESPONDER_PASSWORD = "a long responder password 456!"
TOKEN = "alertmanager-service-test-token"


def workspace(tmp_path):
    database = tmp_path / "workspace.db"
    access = Access(Jobs(database))
    owner = access.create_user("owner",PASSWORD,"administrator",bootstrap=True)
    return database, access, owner


def sign_in(client, username="owner", password=PASSWORD):
    return client.post('/v1/access/login',json={"username":username,"password":password})


def test_roles_enforced_on_every_configuration_path_and_service_key_is_scoped(tmp_path):
    database, access, owner = workspace(tmp_path)
    access.create_user("responder",RESPONDER_PASSWORD,"responder",{"id":owner['id'],"name":"owner"})
    with TestClient(create_app(TOKEN,db_path=database),base_url='http://localhost') as client:
        assert client.get('/v1/config').status_code == 401
        assert client.get('/v1/access/session').json()['enabled']
        assert sign_in(client,'responder',RESPONDER_PASSWORD).status_code == 200
        assert client.get('/v1/config').status_code == 200
        for path in ['/v1/connections','/v1/topology','/v1/sensitivity','/v1/calibration']:
            assert client.post(path,json={}).status_code == 403, path
        assert client.get('/v1/access/users').status_code == 403
        assert client.get('/v1/access/audit').status_code == 403
        assert client.post('/v1/access/users',json={}).status_code == 403
        assert client.patch('/v1/access/users/'+owner['id'],json={'role':'responder','enabled':False}).status_code == 403
        assert client.post('/v1/access/users/'+owner['id']+'/password',json={'password':RESPONDER_PASSWORD}).status_code == 403
        assert client.post('/v1/access/logout').status_code == 200
        assert client.get('/v1/config',headers={'X-API-Key':TOKEN}).status_code == 403
        assert client.get('/v1/access/users',headers={'X-API-Key':TOKEN}).status_code == 403
        hook={'status':'resolved','groupKey':'service-test','alerts':[]}
        assert client.post('/v1/alerts',json=hook,headers={'Authorization':'Bearer '+TOKEN}).status_code == 200
        assert sign_in(client).status_code == 200
        assert client.post('/v1/alerts',json=hook).status_code == 403
        # Responders still reach investigation endpoints; lack of monitoring is not a role denial.
        assert client.post('/v1/diagnoses',json={'inject_time':time.time()}).status_code == 503


def test_sessions_cookie_expiry_revocation_and_last_administrator(tmp_path):
    database, access, owner = workspace(tmp_path)
    responder = access.create_user('responder',RESPONDER_PASSWORD,'responder',{'id':owner['id'],'name':'owner'})
    app = create_app(db_path=database)
    with TestClient(app,base_url='http://localhost') as admin, TestClient(app,base_url='http://localhost') as responder_client:
        reply=sign_in(admin)
        assert 'HttpOnly' in reply.headers['set-cookie'] and 'SameSite=strict' in reply.headers['set-cookie']
        assert COOKIE not in reply.json()
        assert sign_in(responder_client,'responder',RESPONDER_PASSWORD).status_code == 200
        old_token = responder_client.cookies.get(COOKIE)
        assert access.resolve(old_token)
        assert admin.patch('/v1/access/users/'+responder['id'],json={'role':'administrator','enabled':True}).status_code == 200
        assert responder_client.get('/v1/config').status_code == 401
        assert sign_in(responder_client,'responder',RESPONDER_PASSWORD).json()['user']['role']=='administrator'
        assert admin.patch('/v1/access/users/'+responder['id'],json={'role':'responder','enabled':False}).status_code == 200
        assert responder_client.get('/v1/config').status_code == 401
        assert sign_in(responder_client,'responder',RESPONDER_PASSWORD).status_code == 401
        assert admin.patch('/v1/access/users/'+owner['id'],json={'role':'responder','enabled':False}).status_code == 409
        token=admin.cookies.get(COOKIE)
        with access.jobs._db() as db:
            assert token not in str([tuple(r) for r in db.execute('SELECT * FROM sessions')])
            db.execute('UPDATE sessions SET expires_at=0')
        assert admin.get('/v1/config').status_code == 401
        assert sign_in(admin).status_code == 200
        assert admin.post('/v1/access/logout').status_code == 200
        assert admin.get('/v1/config').status_code == 401
    with pytest.raises(ValueError,match='already enabled'):
        access.create_user('another',PASSWORD,'administrator',bootstrap=True)


def test_password_reset_change_and_login_rate_limit_redaction(tmp_path):
    database, access, owner = workspace(tmp_path)
    with TestClient(create_app(db_path=database),base_url='http://localhost') as client:
        assert sign_in(client,password='short').status_code == 401
        assert sign_in(client).status_code == 200
        path='/v1/access/users/'+owner['id']+'/password'
        assert client.post(path,json={'password':RESPONDER_PASSWORD}).status_code == 422
        assert client.post(path,json={'password':RESPONDER_PASSWORD,'current_password':'wrong'}).status_code == 403
        assert client.post(path,json={'password':RESPONDER_PASSWORD,'current_password':PASSWORD}).status_code == 200
        assert client.get('/v1/config').status_code == 401
        assert sign_in(client).status_code == 401
        assert sign_in(client,password=RESPONDER_PASSWORD).status_code == 200
        bad=client.post('/v1/access/users',json={'username':'bad','password':PASSWORD,'role':'invalid'})
        assert bad.status_code==422 and PASSWORD not in bad.text
        events=client.get('/v1/access/audit?limit=2').json()
        assert len(events['events'])==2 and events['next_before']
        older=client.get('/v1/access/audit?before='+str(events['next_before'])).json()['events']
        assert all(e['id']<events['next_before'] for e in older)
        assert PASSWORD not in json.dumps(access.events()) and RESPONDER_PASSWORD not in json.dumps(access.events())
        client.post('/v1/access/logout')
        for _ in range(10):
            assert sign_in(client,'nonexistent',PASSWORD).status_code==401
        assert sign_in(client,'nonexistent',PASSWORD).status_code==429
        with access.jobs._db() as db:
            db.execute('UPDATE login_attempts SET expires_at=0')
        assert sign_in(client,'nonexistent',PASSWORD).status_code==401


def test_config_audit_is_attributed_redacted_persistent_and_atomic(tmp_path):
    database, access, owner = workspace(tmp_path)
    with TestClient(create_app(db_path=database),base_url='http://localhost') as client:
        assert sign_in(client).status_code==200
        source={'prometheus_url':'http://localhost:9090','queries':{'cpu':'secret_query_string'},'services':['cart'],'edges':[]}
        saved=client.post('/v1/connections',json={'source':source})
        assert saved.status_code==200,saved.text
        rev=saved.json()['revision']
        assert client.post('/v1/topology',json={'revision':rev,'services':['cart'],'edges':[]}).status_code==200
        rev=client.get('/v1/integrations').json()['revision']
        assert client.post('/v1/sensitivity',json={'revision':rev,'tau':6}).status_code==200
        events=client.get('/v1/access/audit').json()['events']
        changes=[e for e in events if e['target']=='workspace']
        assert {e['action'] for e in changes}=={'connection.saved','services.saved','sensitivity.saved'}
        assert all(e['actor_id']==owner['id'] and e['actor_name']=='owner' for e in changes)
        assert 'secret_query_string' not in json.dumps(events) and 'localhost:9090' not in json.dumps(events)
        # If the audit insert fails, its configuration update rolls back too.
        with access.jobs._db() as db:
            db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
        old=access.jobs.connection()
        with pytest.raises(sqlite3.IntegrityError):
            access.jobs.save_connection({'source':{},'thresholds':{}},{'actor':{'id':owner['id'],'name':'owner'},'action':'test','target':'workspace'})
        assert access.jobs.connection()==old
    assert len(Access(access.jobs).events())==len(events)


def test_remote_https_cookie_and_cross_origin_rejection(tmp_path):
    database, access, owner = workspace(tmp_path)
    app=create_app(db_path=database)
    with TestClient(app,base_url='http://corca.example.com') as client:
        assert sign_in(client).status_code==400
    with TestClient(app,base_url='https://corca.example.com') as client:
        assert 'Secure' in sign_in(client).headers['set-cookie']
        assert client.get('/v1/config').status_code==200
        assert client.post('/v1/access/logout',headers={'Origin':'https://evil.example'}).status_code==403
        assert client.get('/v1/config').status_code==200
        token=client.cookies.get(COOKIE)
    # Sessions persist across application restart, then are revocable.
    with TestClient(create_app(db_path=database),base_url='https://corca.example.com') as client:
        client.cookies.set(COOKIE,token)
        assert client.get('/v1/config').status_code==200


def test_bootstrap_cli_does_not_requeue_live_work_and_restore_revokes_sessions(tmp_path,monkeypatch):
    from runtime import access as module, admin
    database=tmp_path/'workspace.db'; jobs=Jobs(database)
    job,_=jobs.add('baro',time.time()-1000,300,120)
    assert jobs.claim()['id']==job['id']
    monkeypatch.setattr(module.getpass,'getpass',lambda prompt:PASSWORD)
    monkeypatch.setattr('sys.argv',['access','bootstrap-admin','--db',str(database),'--username','owner'])
    module.main()
    assert jobs.get(job['id'])['status']=='running'
    app=create_app(db_path=database,require_accounts=True)
    with TestClient(app,base_url='http://localhost') as client:
        assert sign_in(client).status_code==200
        admin.copy_database(database,tmp_path/'snapshot.db')
    restored=tmp_path/'restored.db'
    monkeypatch.setattr('sys.argv',['admin','restore','--backup',str(tmp_path/'snapshot.db'),'--db',str(restored)])
    admin.main()
    restored_jobs=object.__new__(Jobs);restored_jobs.path=str(restored)
    with restored_jobs._db() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='workspace.restored'").fetchone()[0]==1
    with pytest.raises(ValueError,match='Individual access is required'):
        create_app(db_path=tmp_path/'empty.db',require_accounts=True)


def test_administrator_can_reset_another_password_and_record_user_creation(tmp_path):
    database,access,owner=workspace(tmp_path)
    app=create_app(db_path=database)
    with TestClient(app,base_url='http://localhost') as admin, TestClient(app,base_url='http://localhost') as person:
        assert sign_in(admin).status_code==200
        user=admin.post('/v1/access/users',json={'username':'person','password':RESPONDER_PASSWORD,'role':'responder'})
        assert user.status_code==201 and 'password' not in user.text
        user=user.json()
        assert sign_in(person,'person',RESPONDER_PASSWORD).status_code==200
        assert admin.post('/v1/access/users/'+user['id']+'/password',json={'password':PASSWORD}).status_code==200
        assert person.get('/v1/config').status_code==401
        assert sign_in(person,'person',RESPONDER_PASSWORD).status_code==401
        assert sign_in(person,'person',PASSWORD).status_code==200
        assert sign_in(admin).status_code==200
        assert admin.post('/v1/access/users',json={'username':'person','password':PASSWORD}).status_code==409
        # No public registration/bootstrap route exists.
        person.post('/v1/access/logout')
        assert person.post('/v1/access/users',json={'username':'outsider','password':PASSWORD}).status_code==401
