"""Account reuse and evidence tests; all profiles, URLs and sessions are synthetic."""
from pathlib import Path
import time
import pytest
from test_app import client
from fastapi import HTTPException
from database import Store
import accounts
import app as service
import worker

URL='https://www.youtube.com/watch?v=abcdefghijk'


def chrome(client,monkeypatch,tmp_path,profile='Default'):
    monkeypatch.setenv('OVD_LOCAL_BROWSER','1');monkeypatch.setattr(accounts.sys,'platform','darwin')
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    for name in ['Default','Profile 2','Profile 99']:
        (tmp_path/'Library/Application Support/Google/Chrome'/name).mkdir(parents=True,exist_ok=True)
    return accounts.connect_chrome(profile,service.STORE)


def member_info():
    return {'availability':'subscriber_only','channel_id':'UCfixture','uploader':'Fixture channel',
            'formats':[{'format_id':'18','type':'video + audio'}],'title':'Fixture',
            'media_key':'youtube:abcdefghijk','suggested_filename':'Fixture'}


def test_profile_exists_is_configuration_not_membership(client,monkeypatch,tmp_path):
    record=chrome(client,monkeypatch,tmp_path)
    monkeypatch.setattr(accounts,'chrome_profiles',lambda:pytest.fail('Normal status must not list profiles'))
    status=client.get('/api/accounts').json()
    assert 'chrome_profiles' not in status
    value=status['accounts'][0]
    assert value['configured'] and value['profile']=='Default' and value['connection_id']==record['connection_id']
    assert value['session_state']=='unknown' and value['membership_state']=='pending' and not value['reconnect_required']
    assert not value['verified_channels']


@pytest.mark.parametrize('url,expected',[(URL,'youtube'),('https://youtu.be/abcdefghijk','youtube'),('https://www.youtube.com.evil.example/watch?v=abcdefghijk',''),('https://example.com/video.mp4',''),('http://www.youtube.com/watch?v=abcdefghijk','')])
def test_auto_provider_scope(client,monkeypatch,tmp_path,url,expected):
    chrome(client,monkeypatch,tmp_path)
    assert accounts.resolve_account(url,'auto',service.STORE,service.CONFIG_DIR)==expected
    assert accounts.resolve_account(url,'',service.STORE,service.CONFIG_DIR)==''
    if not expected:
        with pytest.raises(HTTPException):accounts.worker_credentials(url,'youtube',service.STORE,service.CONFIG_DIR)


def test_preference_and_connection_persist_in_sqlite(client,monkeypatch,tmp_path):
    record=chrome(client,monkeypatch,tmp_path)
    client.put('/api/settings',json={'destination':'transfer','quality':480})
    client.put('/api/settings',json={'account_mode':'public'})
    assert client.get('/api/settings').json()['quality']==480
    other=Store(service.STATE_DIR/'library.sqlite3')
    try:
        assert other.get('account','youtube')['connection_id']==record['connection_id']
        assert accounts.resolve_account(URL,None,other,service.CONFIG_DIR)==''
        assert accounts.worker_credentials(URL,'youtube',other,service.CONFIG_DIR)=={'cookiebrowser':'chrome','cookieprofile':'Default'}
    finally:other.close()


def test_analysis_download_bound_to_same_connection(client,monkeypatch,tmp_path):
    chrome(client,monkeypatch,tmp_path);seen=[]
    async def info(payload,timeout,job=None):seen.append(payload);return member_info()
    monkeypatch.setattr(service,'run_worker',info)
    result=client.post('/api/info',json={'url':URL}).json()
    assert result['account']=='youtube' and seen[0]['cookieprofile']=='Default'
    status=client.get('/api/accounts').json()['accounts'][0]
    assert status['session_state']=='usable' and status['membership_checks']['UCfixture']['evidence']=='analysis'
    assert not status['verified_channels']  # Import still requires a completed authorized download.
    client.post('/api/queue',json={'paused':True})
    request={'url':URL,'analysis_id':result['analysis_id'],'account':'youtube','enqueue':True}
    started=client.post('/api/download/start',json=request)
    assert started.status_code==200
    assert service.JOBS[started.json()['job_id']]['payload']['account']=='youtube'
    mismatch=client.post('/api/download/start',json=request|{'account':''})
    assert mismatch.status_code==409 and 'same connection' in mismatch.json()['detail']
    assert client.post('/api/download/start',json=request|{'url':'https://www.youtube.com/watch?v=anotherabcd'}).status_code==409
    service.STORE.put('account','youtube',accounts.chrome_connection('Profile 2'))
    assert client.post('/api/download/start',json=request).status_code==409


@pytest.mark.parametrize('state',['expired','rejected','blocked'])
def test_expired_or_rejected_auto_is_not_silently_anonymous(client,monkeypatch,tmp_path,state):
    record=chrome(client,monkeypatch,tmp_path);record['session_state']=state;service.STORE.put('account','youtube',record)
    calls=[]
    async def info(payload,timeout,job=None):calls.append(payload);return member_info()
    monkeypatch.setattr(service,'run_worker',info)
    automatic=client.post('/api/info',json={'url':URL,'account':'auto'})
    assert automatic.status_code==409 and not calls
    public=client.post('/api/info',json={'url':URL,'account':''})
    assert public.status_code==200 and public.json()['account']==''
    assert not calls[0].get('cookiebrowser') and not calls[0].get('cookiefile')


def test_non_auth_failures_do_not_force_reauthorization(client,monkeypatch,tmp_path):
    chrome(client,monkeypatch,tmp_path);accounts.verified_download(member_info(),service.STORE)
    for error in ['Insufficient library storage.','YouTube or its media server refused access with HTTP 403.','YouTube rate-limited this request with HTTP 429.','YouTube did not accept this session as a member for this video.','YouTube requires browser verification and refused the downloader session.']:
        accounts.record_failure(error,service.STORE)
        status=accounts.account_status(service.STORE,service.CONFIG_DIR)
        assert status['session_state']=='usable' and not status['reconnect_required']
    accounts.record_failure('The saved YouTube session has expired.',service.STORE)
    assert accounts.account_status(service.STORE,service.CONFIG_DIR)['reconnect_required']


def test_verify_only_selected_profile_and_keep_saved_connection_on_failure(client,monkeypatch,tmp_path):
    record=chrome(client,monkeypatch,tmp_path);monkeypatch.setattr(service.security,'loopback_setup',lambda request:True)
    calls=[]
    async def info(payload,timeout,job=None):calls.append(payload);return {'availability':'public','formats':[{'format_id':'18'}]}
    monkeypatch.setattr(service,'run_worker',info)
    response=client.post('/api/accounts/youtube/browser/verify',json={'profile':'Profile 2','url':URL})
    assert response.status_code==422
    assert service.STORE.get('account','youtube')['connection_id']==record['connection_id']
    assert [item['cookieprofile'] for item in calls]==['Profile 2']
    async def authorized(payload,timeout,job=None):calls.append(payload);return member_info()
    monkeypatch.setattr(service,'run_worker',authorized)
    response=client.post('/api/accounts/youtube/browser/verify',json={'profile':'Profile 2','url':URL})
    assert response.status_code==200  # No file import consent is required for Chrome.
    value=response.json()['account'];assert value['profile']=='Profile 2' and value['session_state']=='usable'
    assert value['connection_id']!=record['connection_id'] and not value['verified_channels']
    assert accounts.worker_credentials(URL,'youtube',service.STORE,service.CONFIG_DIR)['cookieprofile']=='Profile 2'
    assert not (service.CONFIG_DIR/'youtube.cookies.txt').exists()
    assert client.post('/api/accounts/youtube/browser/verify',json={'profile':'Default','url':'https://www.youtube.com/@fixture/videos'}).status_code==400
    assert len(calls)==2


def test_recheck_preserves_download_evidence_and_public_preference(client,monkeypatch,tmp_path):
    record=chrome(client,monkeypatch,tmp_path);accounts.verified_download(member_info(),service.STORE)
    client.put('/api/settings',json={'account_mode':'public'})
    monkeypatch.setattr(service.security,'loopback_setup',lambda request:True)
    async def info(payload,timeout,job=None):return member_info()
    monkeypatch.setattr(service,'run_worker',info)
    response=client.post('/api/accounts/youtube/browser/verify',json={'profile':'Default','url':URL})
    value=response.json()['account'];assert value['connection_id']==record['connection_id']
    assert value['verified_channels']==['UCfixture'] and value['membership_checks']['UCfixture']['evidence']=='download'
    assert client.get('/api/settings').json()['account_mode']=='public'


def test_public_membership_failure_reports_anonymous_request():
    message=worker.source_failure(Exception('Join this channel members-only secret=fixture'),authenticated=False)
    assert 'without a YouTube connection' in message and 'did not accept' not in message and 'fixture' not in message


def test_non_auth_cookie_expiry_does_not_revoke_session(client):
    future=int(time.time())+3600
    text=f'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t{future}\tSID\tsynthetic-session\n.youtube.com\tTRUE\t/\tTRUE\t{future-3000}\tPREF\tsynthetic-pref\n'
    accounts.import_cookies(text,service.CONFIG_DIR,service.STORE)
    assert accounts.account_status(service.STORE,service.CONFIG_DIR)['expires_at']==future


def test_missing_import_does_not_silently_fall_back_to_public(client):
    service.STORE.put('account','youtube',{'provider':'youtube','session_state':'usable','connection_id':'fixture'})
    status=accounts.account_status(service.STORE,service.CONFIG_DIR)
    assert status['configured'] and status['session_state']=='missing' and status['reconnect_required']
    with pytest.raises(HTTPException) as error:
        accounts.resolve_account(URL,'auto',service.STORE,service.CONFIG_DIR)
    assert error.value.status_code==409
    assert accounts.resolve_account(URL,'',service.STORE,service.CONFIG_DIR)==''


@pytest.mark.parametrize('failure',['expired','keychain','signed_out'])
def test_browser_auth_failure_classification_reads_only_selected_profile(monkeypatch,failure):
    from test_browser_connection import cookie
    from yt_dlp.cookies import YoutubeDLCookieJar
    monkeypatch.setenv('OVD_LOCAL_BROWSER','1');monkeypatch.setattr(worker.sys,'platform','darwin')
    seen=[]
    def reader(browser,profile,logger):
        seen.append(profile);jar=YoutubeDLCookieJar()
        if failure=='expired':
            item=cookie('.youtube.com');item.expires=int(time.time())-10;jar.set_cookie(item)
        elif failure=='keychain':logger.warning('find-generic-password failed: synthetic diagnostic')
        return jar
    monkeypatch.setattr(worker,'extract_cookies_from_browser',reader)
    phrase={'expired':'session has expired','keychain':'macOS denied','signed_out':'No signed-in'}[failure]
    with pytest.raises(RuntimeError,match=phrase):
        worker.browser_session({'cookiebrowser':'chrome','cookieprofile':'Default'})
    assert seen==['Default']
