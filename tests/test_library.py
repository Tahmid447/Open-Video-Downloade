"""Integration coverage for persistence, owner access, library boundaries and queue."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
import pytest
from fastapi.testclient import TestClient
import app as service
from database import Store
from extraction import preview, stable_key, member_entries, format_selector
from media import probe
from security import password_hash
from test_app import client, start, wait_status


def download_library(c, url='https://example.com/library', **fields):
    r=c.post('/api/download/start',json={'url':url,'filename':'Library movie','destination':'library',**fields})
    assert r.status_code==200,r.text
    return r.json()


def test_library_persists_separate_from_temporary_cleanup(client):
    job=download_library(client)['job_id']; done=wait_status(client,job,{'completed'})
    assert done['expires_at'] is None and done['library_id']
    item=client.get('/api/library').json()['items'][0]
    path=Path(service.STORE.get('library',item['library_id'])['path'])
    assert path.is_file() and not (service.DOWNLOAD_DIR/job).exists()
    service.JOBS[job]['expires_at']=1
    service.cleanup_stale_jobs(); service.cleanup_orphan_dirs()
    assert path.read_bytes()==b'fixture media'
    assert client.get(item['download_url']).content==b'fixture media'
    assert client.delete('/api/library/'+item['library_id']).status_code==409
    assert client.delete('/api/library/'+item['library_id']+'?confirm=true').json()['ok']
    assert not path.exists() and not client.get('/api/library').json()['items']


def test_range_resume_and_library_metadata(client):
    job=download_library(client)['job_id']; done=wait_status(client,job,{'completed'})
    id=done['library_id']; endpoint='/api/library/'+id
    r=client.get(endpoint+'/file',headers={'Range':'bytes=0-3'})
    assert r.status_code==206 and r.content==b'fixt' and r.headers['content-range']=='bytes 0-3/13'
    assert client.get(endpoint+'/file',headers={'Range':'bytes=999-1000'}).status_code==416
    assert client.head(endpoint+'/file').status_code==200
    assert client.patch(endpoint,json={'collection':'Saved','position':.5,'watched':True}).status_code==200
    items=client.get('/api/library',params={'collection':'Saved','watched':'true','q':'movie'}).json()['items']
    assert items[0]['position']==.5 and items[0]['watched']
    assert not client.get('/api/library',params={'watched':'false'}).json()['items']
    service.LIBRARY_LEASES[id]=1
    assert client.delete(endpoint+'?confirm=true').status_code==409
    service.LIBRARY_LEASES[id]=0


def test_invalid_file_never_completes(client, monkeypatch):
    monkeypatch.setattr(service,'probe',probe)
    job=download_library(client)['job_id']
    failed=wait_status(client,job,{'error'})
    assert 'FFprobe' in failed['error'] and not client.get('/api/library').json()['items']


def test_duplicate_stable_keys_and_force(client):
    assert stable_key('https://youtu.be/abcdefghijk')==stable_key('https://www.youtube.com/watch?v=abcdefghijk&utm_source=a')
    job=download_library(client)['job_id']; wait_status(client,job,{'completed'})
    r=download_library(client)
    assert r['duplicate']
    forced=download_library(client,force=True)['job_id']; wait_status(client,forced,{'completed'})
    assert len(client.get('/api/library').json()['items'])==2


def test_queue_pause_batch_cancel_and_retry(client):
    assert client.post('/api/queue',json={'paused':True}).json()['paused']
    result=client.post('/api/batch',json={'items':[{'url':'https://example.com/one'},{'url':'https://example.com/two'}]}).json()
    ids=[x['job_id'] for x in result['results']]
    assert all(service.JOBS[id]['status']=='queued' for id in ids)
    assert client.post('/api/download/cancel/'+ids[1]).json()['status']=='cancelled'
    assert client.post('/api/queue',json={'paused':False}).status_code==200
    wait_status(client,ids[0],{'completed'})
    retry=client.post('/api/jobs/'+ids[1]+'/retry').json()['job_id']; wait_status(client,retry,{'completed'})
    assert len(client.get('/api/library').json()['items'])==2


def test_restart_durable_history_and_library(client):
    job=download_library(client)['job_id']; done=wait_status(client,job,{'completed'})
    state=service.STORE.path
    # New connection simulates database reopening independently of resident jobs.
    reopened=Store(state)
    assert reopened.get('job',job)['status']=='completed'
    assert reopened.get('library',done['library_id'])['status']=='completed'
    reopened.close()
    service.JOBS.clear()
    assert any(j['job_id']==job for j in client.get('/api/jobs').json()['jobs'])
    assert client.get('/api/library/'+done['library_id']+'/file').status_code==200


def test_library_quota_and_mount_absence(client, monkeypatch):
    monkeypatch.setattr(service,'MAX_LIBRARY_BYTES',1)
    assert client.post('/api/download/start',json={'url':'https://example.com/x'}).status_code==507
    monkeypatch.setenv('LIBRARY_REQUIRE_MOUNT','1')
    assert client.get('/api/health').status_code==503
    with pytest.raises(RuntimeError,match='missing'):
        service.ensure_storage()


def test_restart_recovers_interrupted_and_atomic_save(client):
    # Stored interrupted work is recognized by lifespan, never silently resumed.
    id='a'*32; directory=service.DOWNLOAD_DIR/id;directory.mkdir()
    service.STORE.put('job',id,{'job_id':id,'status':'downloading','created_at':time.time(),'job_dir':str(directory),'requested_name':'Interrupted','media_key':'url:x','payload':{'url':'https://example.com/x'}})
    # Exercise a complete server lifespan with the same isolated volumes.
    original_state=service.STATE_DIR
    reopened_state=original_state.parent/'reopened-state';reopened_state.mkdir()
    service.STORE.backup(reopened_state/'library.sqlite3')
    service.STATE_DIR=reopened_state
    async def reopened():
        original=service.STORE
        # Current fixture has no running jobs; use separate SQLite connection.
        async with service.lifespan(service.app):
            assert service.JOBS[id]['status']=='interrupted'
            assert service.STORE.get('job',id)['status']=='interrupted'
        service.STORE=original
    asyncio.run(reopened())
    service.STATE_DIR=original_state


def test_owner_login_protects_media_and_sessions(client,monkeypatch):
    monkeypatch.setattr(service.security,'local_development',lambda request:False)
    monkeypatch.setenv('COOKIE_SECURE','0')
    service.security.write_owner(service.CONFIG_DIR,{'password_hash':password_hash('example-owner-password')})
    service.security.ATTEMPTS.clear()
    assert client.get('/api/library').status_code==401
    assert client.get('/api/library/'+'a'*32+'/file').status_code==401
    assert client.get('/api/health').status_code==200
    assert client.post('/api/auth/login',json={'password':'wrong'}).status_code==401
    result=client.post('/api/auth/login',json={'password':'example-owner-password'})
    assert result.status_code==200 and 'HttpOnly' in result.headers['set-cookie'] and 'SameSite=strict' in result.headers['set-cookie']
    assert client.get('/api/library').status_code==200
    assert client.post('/api/auth/logout').status_code==200
    assert client.get('/api/library').status_code==401
    bad=client.post('/api/auth/login',json={'password':{'secret':'must-not-echo'}})
    assert bad.status_code==422 and 'must-not-echo' not in bad.text
    assert client.post('/api/auth/login',content='x'*300000).status_code==413


def test_cookie_scope_permissions_gating_and_disconnect(client,monkeypatch):
    import accounts
    monkeypatch.setattr(service.security,'consume_reauthentication',lambda *args:None);monkeypatch.setattr(service,'still_authorized',lambda *args:None)
    good='# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSESSION\tfixture-secret\n'
    async def info(*args):return {'availability':'subscriber_only','channel_id':'UCfixture','formats':[{'format_id':'18'}]}
    monkeypatch.setattr(service,'run_worker',info)
    body={'cookies':good,'authorized':True,'test_url':'https://www.youtube.com/watch?v=abcdefghijk'}
    assert client.post('/api/accounts/youtube',json=body|{'cookies':good.replace('youtube.com','example.com')}).status_code==400
    r=client.post('/api/accounts/youtube',json=body)
    assert r.json()['account']['session_state']=='usable' and 'fixture-secret' not in r.text
    path=accounts.credential_file(r.json()['account'],service.CONFIG_DIR)
    assert path.stat().st_mode & 0o777==0o600 and b'fixture-secret' not in path.read_bytes()
    assert not (service.CONFIG_DIR/'youtube.cookies.txt').exists()
    assert client.post('/api/info',json={'url':'https://example.com/x','account':'youtube'}).status_code==400
    assert client.post('/api/membership/scan',json={'url':'https://www.youtube.com/@channel/membership'}).status_code==409
    assert client.post('/api/membership/scan',json={'url':'https://www.youtube.com/@channel/videos'}).status_code==400
    assert client.delete('/api/accounts/youtube').status_code==200 and not path.exists()


def test_rich_preview_quality_sizes_drm_and_membership_classification():
    info={'title':'Movie','id':'abc','extractor_key':'Example','upload_date':'20261010','duration':1,'subtitles':{'en':[]},'formats':[
      {'format_id':'v','height':1080,'vcodec':'h264','acodec':'none','filesize_approx':123},
      {'format_id':'a','vcodec':'none','acodec':'aac','filesize':42},
      {'format_id':'drm','height':2160,'vcodec':'h264','has_drm':True}]}
    result=preview(info,'https://example.com/x')
    assert result['qualities']==[1080] and result['subtitles']==['en'] and result['audio_available']
    assert result['formats'][0]['filesize_kind']=='estimated' and result['formats'][1]['filesize_kind']=='exact'
    assert 'height<=?720' in format_selector({'quality':720})
    assert preview({'title':None},'https://example.com/x')['title']=='download'
    with pytest.raises(RuntimeError,match='DRM'): preview({'has_drm':True},'https://example.com/x')
    manifest=member_entries({'entries':[{'id':'abcdefghijk','availability':'subscriber_only','title':'Member'},
      {'id':'publicabc12','availability':'public'}, {'id':'unknownab12'}, {'id':'abcdefghijk','availability':'subscriber_only'}]},'https://www.youtube.com/@channel/membership',10)
    assert len(manifest['entries'])==1 and manifest['excluded_unconfirmed']==2 and not manifest['complete']


def test_real_ffprobe_generated_media(tmp_path):
    path=tmp_path/'valid.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=160x90:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-c:a','aac','-shortest','-y',str(path)],check=True)
    result=asyncio.run(probe(path))
    assert result['duration']>0 and {s['codec_type'] for s in result['streams']}=={'video','audio'}
    invalid=tmp_path/'invalid.mp4'; invalid.write_bytes(b'not media')
    with pytest.raises(RuntimeError,match='FFprobe'): asyncio.run(probe(invalid))


def test_schema_and_consistent_backup(tmp_path):
    store=Store(tmp_path/'state.sqlite3');store.put('test','a',{'value':123});store.backup(tmp_path/'backup.sqlite3')
    copied=Store(tmp_path/'backup.sqlite3');assert copied.get('test','a')['value']==123;copied.close()
    store.db.execute('PRAGMA user_version=99');store.close()
    with pytest.raises(RuntimeError,match='newer'): Store(tmp_path/'state.sqlite3')


def test_membership_manifest_dedupe_selection_and_public_exclusion(client):
    # Simulated verified account: deliberately not evidence of real subscription access.
    good='# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSESSION\tfixture\n'
    import accounts
    accounts.import_cookies(good,service.CONFIG_DIR,service.STORE)
    record=service.STORE.get('account','youtube');record.update(status='verified',verified_channels=['UC'+'a'*22]);service.STORE.put('account','youtube',record)
    entries={'youtube:abcdefghijk':{'media_key':'youtube:abcdefghijk','url':'https://www.youtube.com/watch?v=abcdefghijk','title':'Member','availability':'subscriber_only'},
             'youtube:publicabc12':{'media_key':'youtube:publicabc12','url':'https://www.youtube.com/watch?v=publicabc12','title':'Public','availability':'public'}}
    service.STORE.put('manifest','fixture',{'entries':entries,'source':'https://www.youtube.com/@fixture/membership','summary':{}})
    client.post('/api/queue',json={'paused':True})
    response=client.post('/api/membership/import',json={'manifest_id':'fixture','media_keys':list(entries)}).json()
    assert response['results'][0]['job_id'] and response['results'][1]['error']
    job=service.JOBS[response['results'][0]['job_id']]
    assert job['payload']['members_only'] and job['destination']=='library'
    again=client.post('/api/membership/import',json={'manifest_id':'fixture','media_keys':['youtube:abcdefghijk']}).json()
    assert again['results'][0]['duplicate']
    client.post('/api/download/cancel/'+job['job_id'])


def test_password_rotation_and_session_expiry(client,monkeypatch):
    monkeypatch.setattr(service.security,'local_development',lambda request:False)
    monkeypatch.setenv('COOKIE_SECURE','0')
    path=service.CONFIG_DIR/'owner.json'
    service.security.write_owner(service.CONFIG_DIR,{'password_hash':password_hash('old-test-password')})
    service.security.ATTEMPTS.clear()
    assert client.post('/api/auth/login',json={'password':'old-test-password'}).status_code==200
    service.security.write_owner(service.CONFIG_DIR,{'password_hash':password_hash('new-test-password')})
    assert client.get('/api/library').status_code==401
    assert client.post('/api/auth/login',json={'password':'new-test-password'}).status_code==200
    with service.STORE.db:service.STORE.db.execute('UPDATE sessions SET expires=0')
    assert client.get('/api/library').status_code==401


def test_cancel_library_save_removes_only_uncommitted_copy(client):
    job=download_library(client)['job_id'];wait_status(client,job,{'completed'})
    saved=client.get('/api/library').json()['items'][0]
    output=service.DOWNLOAD_DIR/'large.mp4';output.write_bytes(b'x'*2000000)
    interrupted={'job_id':'f'*32,'media_key':'url:fake','cancel_requested':True}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.publish_library(interrupted,output,{}, {'duration':1,'streams':[]}))
    assert len(client.get('/api/library').json()['items'])==1
    assert client.get(saved['play_url']).content==b'fixture media'


def test_crash_after_publication_rolls_forward(client):
    directory=service.LIBRARY_DIR/('c'*32);directory.mkdir();target=directory/'saved.mp4';target.write_bytes(b'fixture media')
    jobid='b'*32
    service.STORE.put('job',jobid,{'job_id':jobid,'status':'interrupted','created_at':time.time(),'requested_name':'Recovered','media_key':'url:crash'})
    service.STORE.put('library','c'*32,{'library_id':'c'*32,'status':'saving','job_id':jobid,'media_key':'url:crash','path':str(target),'filename':target.name,'metadata':{},'size':13,'saved_at':time.time()})
    asyncio.run(service.recover_library())
    assert service.STORE.get('library','c'*32)['status']=='completed'
    assert service.STORE.get('job',jobid)['library_id']=='c'*32


def test_real_yt_dlp_ffmpeg_merge(tmp_path):
    import yt_dlp
    from yt_dlp.postprocessor.ffmpeg import FFmpegMergerPP
    video=tmp_path/'video.mp4';audio=tmp_path/'audio.m4a';output=tmp_path/'merged.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=160x90:d=1','-c:v','libx264','-an','-y',str(video)],check=True)
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','aac','-y',str(audio)],check=True)
    with yt_dlp.YoutubeDL({'quiet':True,'no_warnings':True}) as ydl:
        merger=FFmpegMergerPP(ydl)
        merger.run({'filepath':str(output),'ext':'mp4','__files_to_merge':[str(video),str(audio)],'requested_formats':[
            {'vcodec':'h264','acodec':'none','filepath':str(video),'protocol':'https'},
            {'vcodec':'none','acodec':'aac','filepath':str(audio),'protocol':'https'}]})
    validated=asyncio.run(probe(output))
    assert {s['codec_type'] for s in validated['streams']}=={'video','audio'}


def test_cancel_during_validation_cannot_publish(client,monkeypatch):
    async def slow_probe(path):
        await asyncio.sleep(.4)
        return {'duration':1,'size':path.stat().st_size,'streams':[{'codec_type':'video','height':720}]}
    monkeypatch.setattr(service,'probe',slow_probe)
    job=download_library(client)['job_id']
    wait_status(client,job,{'processing'})
    client.post('/api/download/cancel/'+job)
    assert wait_status(client,job,{'cancelled'})['status']=='cancelled'
    assert not client.get('/api/library').json()['items']
