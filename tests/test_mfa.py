"""Actual owner authentication, not the media fixtures' local auth shortcut."""
import json
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from test_app import client
import app as service
import security

PASSWORD='unit-test-owner-password'
SECRET='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'  # RFC 6238 published test vector, not an owner secret.


def owner(monkeypatch, mfa=False):
    monkeypatch.setattr(security,'local_development',lambda request:False)
    monkeypatch.setenv('COOKIE_SECURE','0')
    data={'password_hash':security.password_hash(PASSWORD)}
    if mfa:data['totp_secret']=SECRET
    security.write_owner(service.CONFIG_DIR,data)


def test_totp_rfc_vectors():
    # RFC 6238 SHA1 vectors reduced to the configured six digits.
    for seconds,expected in [(59,'287082'),(1111111109,'081804'),(1111111111,'050471'),(1234567890,'005924'),(2000000000,'279037'),(20000000000,'353130')]:
        assert security.totp(SECRET,seconds//30)==expected


def test_setup_explicit_loopback_only(client,monkeypatch):
    monkeypatch.setenv('OVD_LOCAL_SETUP','1')
    for address,host,expected in [('127.0.0.1','127.0.0.1:8001',True),('10.0.0.2','127.0.0.1:8001',False),('127.0.0.1','evil.example',False)]:
        assert security.loopback_setup(SimpleNamespace(client=SimpleNamespace(host=address),headers={'host':host}))==expected
    c=TestClient(service.app,base_url='http://127.0.0.1:8001',client=('127.0.0.1',1234))
    assert c.post('/api/auth/setup',json={'password':'short'}).status_code==400
    assert c.post('/api/auth/setup',json={'password':PASSWORD}).status_code==200
    digest=security.owner_hash(service.CONFIG_DIR)
    assert c.post('/api/auth/setup',json={'password':'replacement-test-password'}).status_code==403
    assert security.owner_hash(service.CONFIG_DIR)==digest
    assert (service.CONFIG_DIR/'owner.json').stat().st_mode&0o777==0o600
    monkeypatch.setenv('OVD_LOCAL_SETUP','0')
    assert not security.loopback_setup(SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),headers={'host':'127.0.0.1:8001'}))


def test_mfa_enrollment_revokes_password_session(client,monkeypatch):
    owner(monkeypatch); monkeypatch.setenv('OVD_REQUIRE_MFA','1')
    assert client.post('/api/auth/login',json={'password':PASSWORD}).status_code==200
    assert client.get('/api/library').status_code==403
    assert client.post('/api/auth/mfa/start',json={'password':'incorrect'}).status_code==401
    enrollment=client.post('/api/auth/mfa/start',json={'password':PASSWORD}).json()
    assert enrollment['secret'] in enrollment['uri']
    assert client.post('/api/auth/mfa/confirm',json={'password':PASSWORD,'code':'abcdef'}).status_code==400
    code=security.totp(enrollment['secret'],int(time.time()//30))
    assert client.post('/api/auth/mfa/confirm',json={'password':PASSWORD,'code':code}).status_code==200
    assert client.get('/api/library').status_code==401
    assert client.post('/api/auth/login',json={'password':PASSWORD}).status_code==401
    assert client.post('/api/auth/login',json={'password':PASSWORD,'code':code}).status_code==401  # Enrollment code cannot be replayed.
    time_value=(int(time.time()//30)+2)*30
    monkeypatch.setattr(security.time,'time',lambda:time_value)
    fresh=security.totp(enrollment['secret'],int(time_value//30))
    assert client.post('/api/auth/login',json={'password':PASSWORD,'code':fresh}).status_code==200
    assert client.get('/api/library').status_code==200
    assert client.post('/api/auth/login',json={'password':PASSWORD,'code':fresh}).status_code==401


def test_all_sensitive_endpoints_deny_anonymous(client,monkeypatch):
    owner(monkeypatch,mfa=True)
    for method,path,body in [('GET','/api/library',None),('HEAD','/api/library/anything/file',None),('GET','/api/download/file/anything',None),('GET','/api/jobs',None),('GET','/api/settings',None),('GET','/api/accounts',None),('POST','/api/auth/mfa/start',{'password':PASSWORD}),('POST','/api/info',{'url':'https://example.com'}),('POST','/api/download/start',{'url':'https://example.com'}),('POST','/api/membership/scan',{'url':'https://www.youtube.com/@channel/membership'})]:
        assert client.request(method,path,json=body).status_code==401
    assert client.get('/api/health').status_code==200
    assert client.get('/docs').status_code==404


def test_secure_cookie_https_and_cross_scheme_csrf(client,monkeypatch):
    owner(monkeypatch); monkeypatch.setenv('OVD_REQUIRE_HTTPS','1'); monkeypatch.setenv('COOKIE_SECURE','1')
    assert client.post('/api/auth/login',json={'password':PASSWORD}).status_code==403
    secure=TestClient(service.app,base_url='https://testserver')
    result=secure.post('/api/auth/login',json={'password':PASSWORD})
    assert result.status_code==200
    cookie=result.headers['set-cookie'].lower()
    assert 'secure' in cookie and 'httponly' in cookie and 'samesite=strict' in cookie
    assert secure.post('/api/queue',json={'paused':True},headers={'Origin':'http://testserver'}).status_code==403
    assert secure.post('/api/queue',json={'paused':True},headers={'Origin':'https://evil.example'}).status_code==403
    assert secure.get('/api/library').status_code==200


def test_rate_limit_is_durable(client,monkeypatch):
    owner(monkeypatch)
    for _ in range(10):assert client.post('/api/auth/login',json={'password':'incorrect'}).status_code==401
    assert len(service.STORE.get('security','attempts')['times'])==10
    assert client.post('/api/auth/login',json={'password':PASSWORD}).status_code==429
    # A second database connection observes the throttle; restart cannot clear it.
    from database import Store
    another=Store(service.STORE.path)
    try:assert len(another.get('security','attempts')['times'])==10
    finally:another.close()


def test_expired_enrollment_cannot_enable_mfa(client,monkeypatch):
    owner(monkeypatch)
    assert client.post('/api/auth/login',json={'password':PASSWORD}).status_code==200
    seed=client.post('/api/auth/mfa/start',json={'password':PASSWORD}).json()['secret']
    data=security.owner_config(service.CONFIG_DIR);data['totp_pending_expires']=1;security.write_owner(service.CONFIG_DIR,data)
    assert client.post('/api/auth/mfa/confirm',json={'password':PASSWORD,'code':security.totp(seed,int(time.time()//30))}).status_code==400
    assert not security.mfa_ready(service.CONFIG_DIR)
