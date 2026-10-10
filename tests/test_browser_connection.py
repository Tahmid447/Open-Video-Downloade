"""Scoped, ephemeral Chrome sessions; all cookie values here are synthetic."""
import asyncio
import http.cookiejar
from pathlib import Path
import pytest
from fastapi import HTTPException
from test_app import client
import accounts
import app as service
import worker
from yt_dlp.cookies import YoutubeDLCookieJar


def cookie(domain,name='SID',value='synthetic-session'):
    return http.cookiejar.Cookie(0,name,value,None,False,domain,True,domain.startswith('.'),'/',True,True,None,True,None,None,{},False)


def test_browser_jar_is_scoped_memory_only(monkeypatch):
    monkeypatch.setenv('OVD_LOCAL_BROWSER','1');monkeypatch.setattr(worker.sys,'platform','darwin')
    source=YoutubeDLCookieJar();source.set_cookie(cookie('.youtube.com'));source.set_cookie(cookie('.example.com',value='unrelated-synthetic'));source.set_cookie(cookie('.google.com'))
    monkeypatch.setattr(worker,'extract_cookies_from_browser',lambda name,profile,logger:source)
    jar=worker.browser_session({'cookiebrowser':'chrome','cookieprofile':'Default'})
    assert jar.filename is None and len(jar)==1 and len(source)==0
    assert jar.get_cookie_header('https://www.youtube.com/')=='SID=synthetic-session'
    assert not jar.get_cookie_header('http://www.youtube.com/')
    assert not jar.get_cookie_header('https://www.example.com/')
    assert not jar.get_cookie_header('https://accounts.google.com/')


def test_browser_reader_refuses_remote_or_arbitrary_profile(monkeypatch):
    monkeypatch.setenv('OVD_LOCAL_BROWSER','0')
    with pytest.raises(RuntimeError,match='disabled'):worker.browser_session({'cookiebrowser':'chrome'})
    monkeypatch.setenv('OVD_LOCAL_BROWSER','1');monkeypatch.setattr(worker.sys,'platform','darwin')
    with pytest.raises(RuntimeError,match='Invalid'):worker.browser_session({'cookiebrowser':'chrome','cookieprofile':'../../private'})
    monkeypatch.setattr(worker.sys,'platform','linux')
    with pytest.raises(RuntimeError,match='disabled'):worker.browser_session({'cookiebrowser':'chrome'})


@pytest.mark.parametrize('exception,category',[(FileNotFoundError('private-sensitive-path'),'database'),(PermissionError('sensitive-token'),'macOS denied'),(ValueError('private-sensitive-value'),'could not be read')])
def test_browser_errors_do_not_echo_private_data(monkeypatch,exception,category):
    monkeypatch.setenv('OVD_LOCAL_BROWSER','1');monkeypatch.setattr(worker.sys,'platform','darwin')
    def fail(*args,**kwargs):raise exception
    monkeypatch.setattr(worker,'extract_cookies_from_browser',fail)
    with pytest.raises(RuntimeError,match=category) as caught:worker.browser_session({'cookiebrowser':'chrome'})
    assert str(exception) not in str(caught.value)


def test_connection_metadata_contains_no_export(client,monkeypatch,tmp_path):
    monkeypatch.setenv('OVD_LOCAL_BROWSER','1');monkeypatch.setattr(accounts.sys,'platform','darwin');monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    (tmp_path/'Library/Application Support/Google/Chrome/Default').mkdir(parents=True)
    record=accounts.connect_chrome('Default',service.STORE)
    assert record['source']=='browser' and record['status']=='unverified'
    credentials=accounts.worker_credentials('https://www.youtube.com/watch?v=abcdefghijk','youtube',service.STORE,service.CONFIG_DIR)
    assert credentials=={'cookiebrowser':'chrome','cookieprofile':'Default'}
    assert not (service.CONFIG_DIR/'youtube.cookies.txt').exists()
    with pytest.raises(HTTPException):accounts.worker_credentials('https://example.com','youtube',service.STORE,service.CONFIG_DIR)
    monkeypatch.setenv('OVD_LOCAL_BROWSER','0')
    assert accounts.account_status(service.STORE,service.CONFIG_DIR)['status']=='disconnected'


def test_browser_endpoint_is_local_only(client,monkeypatch):
    monkeypatch.setattr(service.security,'loopback_setup',lambda request:False)
    assert client.post('/api/accounts/youtube/browser',json={'profile':'Default'}).status_code==403


def test_cancel_removes_browser_read_directory(client):
    async def scenario():
        task=asyncio.create_task(service.run_worker({'mode':'info','url':'https://example.com/slowinfo','cookiebrowser':'chrome'},20))
        await asyncio.sleep(.1)
        assert list(service.CONFIG_DIR.glob('browser-read-*'))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not list(service.CONFIG_DIR.glob('browser-read-*'))
    asyncio.run(scenario())


@pytest.mark.parametrize('raw,phrase',[("sign in to confirm you're not a bot; secret=private-fixture",'browser verification'),('Join this channel members-only; token=private-fixture','did not accept'),('HTTP Error 403; Cookie: private-fixture','HTTP 403'),('PO Token required private-fixture','proof-of-origin')])
def test_precise_source_failure_never_echoes_tokens(raw,phrase):
    result=worker.source_failure(Exception(raw))
    assert phrase in result and 'private-fixture' not in result
