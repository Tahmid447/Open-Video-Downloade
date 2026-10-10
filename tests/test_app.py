import asyncio
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as service
import common


@pytest.fixture
def client(monkeypatch, tmp_path):
    directory = tmp_path/'downloads'; directory.mkdir()
    worker_dir = tmp_path/'worker'; worker_dir.mkdir()
    shutil.copy(Path(__file__).parent/'fake_worker.py', worker_dir/'worker.py')
    # Common is made available to the independent worker without changing sys.path in production.
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv('CHILD_PID_FILE', str(tmp_path/'child.pid'))
    monkeypatch.setattr(service, 'BASE_DIR', worker_dir)
    monkeypatch.setattr(service, 'DOWNLOAD_DIR', directory)
    monkeypatch.setattr(service, 'dependencies_ready', lambda: True)
    monkeypatch.setattr(service, 'validate_remote_url', lambda url: url)
    monkeypatch.setattr(service, 'MIN_FREE_DISK_BYTES', 0)
    monkeypatch.setattr(service, 'MAX_TEMP_BYTES_PER_JOB', 1_000_000)
    monkeypatch.setattr(service, 'MAX_STORAGE_BYTES', 20_000_000)
    monkeypatch.setattr(service, 'JOB_TIMEOUT_SECONDS', 5)
    service.JOBS.clear(); service.TASKS.clear(); service.PROCESSES.clear()
    with TestClient(service.app) as c:
        yield c
    assert not service.PROCESSES
    assert not service.TASKS


def start(client, scenario='complete', name='My movie.mp4', **kwargs):
    return client.post('/api/download/start', json={'url': f'https://example.com/{scenario}', 'filename': name, **kwargs})


def wait_status(client, job_id, statuses, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=client.get('/api/download/progress/'+job_id)
        if result.status_code==200 and result.json()['status'] in statuses:
            return result.json()
        time.sleep(.03)
    raise AssertionError(f'Expected {statuses}, got {result.text}')


def test_health_and_readiness(client, monkeypatch):
    assert client.get('/api/health').json()['ok']
    monkeypatch.setattr(service,'dependencies_ready',lambda: False)
    assert client.get('/api/health').status_code==503
    assert start(client).status_code==503
    assert client.post('/api/info',json={'url':'https://example.com/video'}).status_code==503


def test_info_and_download_flow(client):
    info=client.post('/api/info',json={'url':'https://example.com/video'})
    assert info.status_code==200 and info.json()['formats'][0]['format_id']=='18'
    response=start(client,name='Edited before.mp4',format_id='18')
    assert response.status_code==200
    job=response.json()['job_id']
    progress=wait_status(client,job,{'downloading'})
    assert progress['speed']==5 and progress['eta']==1 and progress['total_bytes']==10
    done=wait_status(client,job,{'completed'})
    assert done['filename']=='Edited before.mp4' and done['expires_at']>time.time()
    served=client.get(done['download_url'],params={'name':'Renamed after.mp4'})
    assert served.content==b'fixture media'
    assert 'Renamed%20after.mp4' in served.headers['content-disposition']
    assert set(p.name for p in Path(service.JOBS[job]['job_dir']).iterdir())=={'Edited before.mp4'}
    expiry=service.JOBS[job]['expires_at']
    client.get(done['download_url'])
    assert service.JOBS[job]['expires_at']==expiry
    assert client.get('/api/jobs').json()['jobs'][0]['job_id']==job
    assert client.delete('/api/jobs/'+job).status_code==200
    assert client.get(done['download_url']).status_code==404
    assert not (service.DOWNLOAD_DIR/job).exists()


def test_limit_concurrency_and_cancel_cleanup(client):
    job=start(client,'hang').json()['job_id']
    wait_status(client,job,{'downloading'})
    assert start(client).status_code==429
    assert client.delete('/api/jobs/'+job).status_code==409
    assert client.post('/api/download/cancel/'+job).json()['status']=='cancelling'
    assert wait_status(client,job,{'cancelled'})['error'] is None
    assert not (service.DOWNLOAD_DIR/job).exists()
    assert client.post('/api/download/cancel/'+job).json()['status']=='cancelled'


def process_is_live(pid):
    result=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


def test_ffmpeg_child_cancelled(client):
    job=start(client,'processing').json()['job_id']
    wait_status(client,job,{'processing'})
    pid=int(Path(os.environ['CHILD_PID_FILE']).read_text())
    assert process_is_live(pid)
    client.post('/api/download/cancel/'+job)
    wait_status(client,job,{'cancelled'})
    deadline=time.monotonic()+2
    while process_is_live(pid) and time.monotonic()<deadline: time.sleep(.05)
    assert not process_is_live(pid)
    assert not (service.DOWNLOAD_DIR/job).exists()


def test_timeout_removes_partials(client, monkeypatch):
    monkeypatch.setattr(service,'JOB_TIMEOUT_SECONDS',.4)
    job=start(client,'hang').json()['job_id']
    failed=wait_status(client,job,{'error'})
    assert 'timed out' in failed['error']
    assert not (service.DOWNLOAD_DIR/job).exists()


def test_failure_cleanup(client):
    job=start(client,'error').json()['job_id']
    assert wait_status(client,job,{'error'})['error']=='Fixture failed'
    assert not (service.DOWNLOAD_DIR/job).exists()


def test_final_size_limit(client,monkeypatch):
    monkeypatch.setattr(service,'MAX_FILESIZE',100)
    job=start(client,'oversize').json()['job_id']
    assert 'maximum' in wait_status(client,job,{'error'})['error']
    assert not (service.DOWNLOAD_DIR/job).exists()


def test_disk_admission_and_during_job(client,monkeypatch):
    actual=shutil.disk_usage(service.DOWNLOAD_DIR)
    monkeypatch.setattr(service.shutil,'disk_usage',lambda _path: actual._replace(free=0))
    assert start(client).status_code==507
    assert client.get('/api/jobs').json()['storage_warning']
    monkeypatch.setattr(service.shutil,'disk_usage',lambda _path: actual)
    job=start(client,'hang').json()['job_id']
    wait_status(client,job,{'downloading'})
    monkeypatch.setattr(service,'MIN_FREE_DISK_BYTES',actual.free+1)
    failed=wait_status(client,job,{'error'})
    assert 'Insufficient storage' in failed['error']
    assert not (service.DOWNLOAD_DIR/job).exists()


def test_temp_quota(client,monkeypatch):
    job=start(client,'hang').json()['job_id']
    wait_status(client,job,{'downloading'})
    monkeypatch.setattr(service,'MAX_TEMP_BYTES_PER_JOB',1)
    assert 'temporary storage limit' in wait_status(client,job,{'error'})['error']
    assert not (service.DOWNLOAD_DIR/job).exists()


def test_history_bounded(client,monkeypatch):
    monkeypatch.setattr(service,'MAX_JOB_HISTORY',2)
    jobs=[]
    for _ in range(3):
        job=start(client).json()['job_id']; wait_status(client,job,{'completed'}); jobs.append(job)
    assert len(service.JOBS)==2 and jobs[0] not in service.JOBS
    assert not (service.DOWNLOAD_DIR/jobs[0]).exists()


def test_expiry_active_exclusion_and_orphans(client):
    job=start(client).json()['job_id']; wait_status(client,job,{'completed'})
    service.JOBS[job]['expires_at']=time.time()-1
    assert client.get('/api/download/progress/'+job).status_code==404
    assert not (service.DOWNLOAD_DIR/job).exists()
    active=start(client,'hang').json()['job_id']; wait_status(client,active,{'downloading'})
    old=time.time()-99999; os.utime(service.DOWNLOAD_DIR/active,(old,old))
    orphan=service.DOWNLOAD_DIR/('a'*32); orphan.mkdir(); (orphan/'x.part').write_bytes(b'x')
    unrelated=service.DOWNLOAD_DIR/'leave-alone'; unrelated.mkdir()
    service.cleanup_stale_jobs(); service.cleanup_orphan_dirs()
    assert (service.DOWNLOAD_DIR/active).exists() and not orphan.exists() and unrelated.exists()
    client.post('/api/download/cancel/'+active)
    wait_status(client,active,{'cancelled'})


def test_serving_lease_prevents_expiry_and_delete(client):
    job=start(client).json()['job_id']; wait_status(client,job,{'completed'})
    service.JOBS[job]['leases']=1
    service.JOBS[job]['expires_at']=time.time()-1
    service.cleanup_stale_jobs()
    assert job in service.JOBS
    assert client.delete('/api/jobs/'+job).status_code==409
    service.JOBS[job]['leases']=0
    service.cleanup_stale_jobs()
    assert job not in service.JOBS


def test_bad_input_and_csrf(client):
    assert start(client,format_id='18; rm -rf /').status_code==400
    assert client.post('/api/download/start',json={'url':'bad'}).status_code==422
    assert client.get('/api/download/file/../../etc/passwd').status_code==404
    assert client.get('/api/download/progress/'+'b'*32).status_code==404
    assert client.post('/api/download/start',json={'url':'https://example.com/v'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/download/start',json={'url':'https://example.com/v'},headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.get('/api/health').headers['cache-control']=='no-store'
    assert "frame-ancestors 'none'" in client.get('/api/health').headers['content-security-policy']


@pytest.mark.parametrize('name', ['../../etc/passwd.mp4','a\\b\x00.mp4','CON.mp4','..','', '日本語'*120, '%(title)s.mp4'])
def test_filename_sanitization(name):
    safe=common._sanitize_stem(name)
    assert safe and not any(c in safe for c in '/\\\x00')
    assert not safe.endswith('.') and len(safe.encode())<=180
    if name=='CON.mp4': assert safe=='_CON'
    if name.endswith('%(title)s.mp4'): assert safe=='%(title)s'


@pytest.mark.parametrize('url',['http://127.0.0.1','http://localhost','http://192.168.1.1','http://169.254.169.254','http://[::1]','http://[::ffff:127.0.0.1]','http://100.64.0.1','https://user:pass@example.com','ftp://example.com','http://example.com:8000'])
def test_reject_unsafe_url(url):
    with pytest.raises(HTTPException): common.validate_remote_url(url)


def test_public_url_and_mixed_dns():
    with patch.object(common.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]):
        assert common.validate_remote_url('https://example.com/video')=='https://example.com/video'
    with patch.object(common.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',443))]):
        with pytest.raises(HTTPException): common.validate_remote_url('https://example.com')


def test_socket_guard_blocks_actual_connections():
    code='''
import socket
from network_guard import install
install()
for address in [('127.0.0.1',80),('169.254.169.254',80),('10.0.0.1',443),('93.184.216.34',22)]:
    s=socket.socket()
    try: s.connect(address)
    except OSError as e: assert 'blocked' in str(e) or 'standard web' in str(e)
    else: raise AssertionError('Unsafe destination permitted')
    finally: s.close()
try: socket.getaddrinfo('localhost',80)
except OSError: pass
else: raise AssertionError('Local DNS permitted')
'''
    result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_progress_calculation_and_formats():
    assert common._progress_percent({'downloaded_bytes':5,'total_bytes':10})==50
    assert common._progress_percent({'fragment_index':1,'fragment_count':4})==25
    assert common._progress_percent({}) is None
    formats=common.clean_formats({'formats':[{'format_id':'18','vcodec':'h264','acodec':'aac','height':720}, {'format_id':'18','vcodec':'h264','acodec':'aac','height':720}]})
    assert len(formats)==1 and formats[0]['type']=='video + audio'


def test_shutdown_kills_active_job(monkeypatch,tmp_path):
    with TestClient(service.app) as c:
        # Separate from fixture: real fake-worker path, no live Mac port involved.
        worker_dir=tmp_path/'w'; worker_dir.mkdir(); shutil.copy(Path(__file__).parent/'fake_worker.py',worker_dir/'worker.py')
        monkeypatch.setattr(service,'BASE_DIR',worker_dir)
        monkeypatch.setattr(service,'DOWNLOAD_DIR',tmp_path/'d'); service.DOWNLOAD_DIR.mkdir()
        monkeypatch.setattr(service,'MIN_FREE_DISK_BYTES',0)
        monkeypatch.setattr(service,'MAX_TEMP_BYTES_PER_JOB',1_000_000)
        monkeypatch.setattr(service,'validate_remote_url',lambda u:u)
        monkeypatch.setattr(service,'dependencies_ready',lambda:True)
        monkeypatch.setenv('PYTHONPATH',str(Path(__file__).resolve().parents[1]))
        monkeypatch.setenv('CHILD_PID_FILE',str(tmp_path/'pid'))
        service.JOBS.clear()
        job=start(c,'hang').json()['job_id']; wait_status(c,job,{'downloading'})
    assert not service.PROCESSES and not service.TASKS
    assert service.JOBS[job]['status']=='cancelled' and not (service.DOWNLOAD_DIR/job).exists()


def test_disconnected_transfer_releases_lease(client):
    job=start(client).json()['job_id']; wait_status(client,job,{'completed'})
    async def exercise():
        response=await service.download_file(job)
        assert service.JOBS[job]['leases']==1
        async def receive(): return {'type':'http.disconnect'}
        async def send(_message): raise RuntimeError('Client disconnected')
        with pytest.raises(RuntimeError,match='disconnected'):
            await response({'type':'http','method':'GET','headers':[],'extensions':{}},receive,send)
    asyncio.run(exercise())
    assert service.JOBS[job]['leases']==0
