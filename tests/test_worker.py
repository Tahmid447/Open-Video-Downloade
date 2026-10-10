"""Exercise the real worker/options in a subprocess with yt-dlp's network mocked."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
CODE=r'''
import json, pathlib, sys
import worker
payload=json.loads(sys.argv[1])
class FakeYDL:
    def __init__(self,opts): self.opts=opts
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def extract_info(self,url,download=False):
        if payload['mode']=='scan':
            return {'channel_id':payload.get('channel_id','UCfixture'),'entries':[
                {'id':'abcdefghijk','availability':'subscriber_only','title':'Member'},
                {'id':'publicabc12','availability':'public','title':'Public'},
                {'id':'unknownab12','title':'Unknown'}]}
        return {'title':'日本語 Movie','availability':payload.get('availability','public'),'is_live':payload.get('live',False),'formats':[{'format_id':'18','vcodec':'h264','acodec':'aac','height':720}]}
    def process_ie_result(self,info,download=True):
        assert self.opts['outtmpl'].endswith('media.%(ext)s')
        assert self.opts['continuedl'] is False
        assert self.opts['cachedir'] is False and self.opts['remote_components']==set()
        if payload['audio_only']:
            assert self.opts['postprocessors'][0]['key']=='FFmpegExtractAudio'
            ext='m4a'
        else:
            assert self.opts['format']==(payload['format_id'] or 'bestvideo+bestaudio/best')
            ext='mp4'
        for hook in self.opts['progress_hooks']:
            hook({'status':'downloading','downloaded_bytes':50,'total_bytes':100,'speed':25,'eta':2})
            hook({'status':'finished','downloaded_bytes':100})
        for hook in self.opts['postprocessor_hooks']: hook({'status':'started'})
        output=pathlib.Path(self.opts['outtmpl'].replace('%(ext)s',ext)); output.write_bytes(b'x'*2000 if payload.get('oversize') else b'media fixture')
worker.yt_dlp.YoutubeDL=FakeYDL
worker.execute(payload)
'''


def run(payload, extra_env=None):
    env=os.environ.copy(); env.update(extra_env or {})
    return subprocess.run([sys.executable,'-c',CODE,json.dumps(payload)],cwd=ROOT,capture_output=True,text=True,env=env)


def test_real_worker_analysis():
    result=run({'mode':'info','url':'https://example.com/video'})
    assert result.returncode==0,result.stderr
    data=json.loads(result.stdout)['data']
    assert data['suggested_filename']=='日本語 Movie' and data['formats'][0]['format_id']=='18'


@pytest.mark.parametrize('audio,format_id',[(False,''),(False,'18'),(True,'')])
def test_real_worker_formats_rename_and_events(tmp_path,audio,format_id):
    result=run({'mode':'download','url':'https://example.com/video','job_dir':str(tmp_path),'requested_name':'../%(title)s日本語.mp4','audio_only':audio,'format_id':format_id})
    assert result.returncode==0,result.stderr
    events=[json.loads(line) for line in result.stdout.splitlines()]
    assert next(e for e in events if e['type']=='progress' and e['data']['status']=='downloading')['data']['progress']==50
    assert any(e.get('data',{}).get('status')=='processing' for e in events)
    file=Path(events[-1]['data']['file_path'])
    assert file.name=='%(title)s日本語'+('.m4a' if audio else '.mp4')
    assert file.read_bytes()==b'media fixture' and file.parent==tmp_path


def test_live_stream_rejected():
    result=run({'mode':'info','url':'https://example.com/live','live':True})
    assert result.returncode!=0 and 'Live streams' in result.stderr


def test_os_size_limit_for_unknown_size(tmp_path):
    result=run({'mode':'download','url':'https://example.com/video','job_dir':str(tmp_path),'requested_name':'Unknown size','audio_only':False,'format_id':'','oversize':True}, {'MAX_FILESIZE_BYTES':'100'})
    assert result.returncode!=0 and 'File too large' in result.stderr
    assert (tmp_path/'media.mp4').stat().st_size<=100


def test_worker_scan_verification_and_classification():
    result=run({'mode':'scan','url':'https://www.youtube.com/@fixture/membership','limit':10,'verified_channels':['UCfixture']})
    assert result.returncode==0,result.stderr
    data=json.loads(result.stdout)['data']
    assert len(data['entries'])==1 and data['excluded_unconfirmed']==2 and not data['complete']
    denied=run({'mode':'scan','url':'https://www.youtube.com/@other/membership','limit':10,'verified_channels':['UCother']})
    assert denied.returncode!=0 and 'specific channel' in denied.stderr


def test_import_rechecks_members_only_before_transfer(tmp_path):
    result=run({'mode':'download','url':'https://www.youtube.com/watch?v=abcdefghijk','job_dir':str(tmp_path),'requested_name':'Member','audio_only':False,'format_id':'','members_only':True,'availability':'public'})
    assert result.returncode!=0 and 'not downloaded' in result.stderr
    assert not list(tmp_path.iterdir())


def test_worker_cookie_file_is_loaded_without_rewriting(tmp_path):
    path=tmp_path/'session.txt'
    original='# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSESSION\tfixture-secret\n'
    path.write_text(original)
    result=run({'mode':'info','url':'https://www.youtube.com/watch?v=abcdefghijk','cookiefile':str(path)})
    assert result.returncode==0,result.stderr
    assert path.read_text()==original and 'fixture-secret' not in result.stdout


def test_session_cookies_never_sent_over_http(tmp_path):
    from worker import SecureSessionPolicy
    from yt_dlp.cookies import YoutubeDLCookieJar
    path=tmp_path/'cookies.txt';path.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tSESSION\tfixture\n')
    jar=YoutubeDLCookieJar(str(path));jar.load(ignore_discard=True);jar.set_policy(SecureSessionPolicy())
    assert jar.get_cookie_header('https://www.youtube.com/')=='SESSION=fixture'
    assert not jar.get_cookie_header('http://www.youtube.com/')
