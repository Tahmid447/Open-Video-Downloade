"""Test fixture only: never included in production images/uploads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

payload = json.loads(sys.stdin.readline())
def emit(kind, data):
    print(json.dumps({"type": kind, "data": data}), flush=True)
if payload['mode'] == 'info':
    if payload['url'].endswith('/slowinfo'): time.sleep(60)
    emit('result', {'title': 'Fixture', 'suggested_filename': 'Fixture', 'thumbnail': None,
                    'formats': [{'format_id': '18', 'type': 'video + audio'}]})
    sys.exit(0)
directory = Path(payload['job_dir'])
(directory/'media.mp4.part').write_bytes(b'partial')
scenario = payload['url'].split('/')[-1]
if scenario in {'hang', 'processing'}:
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
    Path(os.environ['CHILD_PID_FILE']).write_text(str(child.pid))
    emit('progress', {'status': 'processing' if scenario == 'processing' else 'downloading', 'progress': 42, 'downloaded_bytes': 42, 'total_bytes': 100, 'speed': 10, 'eta': 6})
    time.sleep(60)
if scenario == 'error':
    emit('error', 'Fixture failed')
    sys.exit(1)
if scenario == 'oversize':
    (directory/'media.mp4').write_bytes(b'x'*2048)
else:
    (directory/'media.mp4').write_bytes(b'fixture media')
emit('progress', {'status': 'downloading', 'progress': 50, 'downloaded_bytes': 5, 'total_bytes': 10, 'speed': 5, 'eta': 1})
time.sleep(.15)
emit('progress', {'status': 'processing', 'progress': 100})
time.sleep(.15)
output = directory/'media.mp4'
from common import _sanitize_stem
final = directory/(_sanitize_stem(payload['requested_name']) + '.mp4')
output.rename(final)
emit('result', {'file_path': str(final)})
