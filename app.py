"""Owner-only downloader and persistent library. Run one scheduler/Uvicorn worker."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import mimetypes
import time
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl
from typing import Literal
from fastapi.exceptions import RequestValidationError
from database import Store
from extraction import stable_key
from media import probe
import security
import accounts

from common import MAX_FILESIZE, MEDIA_EXTENSIONS, _sanitize_stem, validate_remote_url

BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "Open Video Downloader"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(Path(tempfile.gettempdir()) / "ovd-cloud"))).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = Path(os.getenv("STATE_DIR", str(DOWNLOAD_DIR.parent / "ovd-next-state"))).resolve()
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", str(DOWNLOAD_DIR.parent / "ovd-next-config"))).resolve()
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", str(DOWNLOAD_DIR.parent / "ovd-next-library"))).resolve()
MAX_LIBRARY_BYTES = int(os.getenv("MAX_LIBRARY_BYTES", str(10 * 1024**3)))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "100"))
STORE = None
LIBRARY_LEASES = {}
QUEUE_PAUSED = False
LAST_START = 0.0
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "7200"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("JOB_CLEANUP_INTERVAL_SECONDS", "30"))
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))
INFO_TIMEOUT_SECONDS = int(os.getenv("INFO_TIMEOUT_SECONDS", "90"))
MAX_SIMULTANEOUS_JOBS = int(os.getenv("MAX_SIMULTANEOUS_JOBS", "1"))
MAX_JOB_HISTORY = int(os.getenv("MAX_JOB_HISTORY", "30"))
MIN_FREE_DISK_BYTES = int(os.getenv("MIN_FREE_DISK_BYTES", str(1024**3)))
MAX_TEMP_BYTES_PER_JOB = int(os.getenv("MAX_TEMP_BYTES_PER_JOB", str(3 * MAX_FILESIZE)))
MAX_STORAGE_BYTES = int(os.getenv("MAX_STORAGE_BYTES", str(6 * 1024**3)))
for value in (MAX_FILESIZE, JOB_TTL_SECONDS, CLEANUP_INTERVAL_SECONDS, JOB_TIMEOUT_SECONDS,
              INFO_TIMEOUT_SECONDS, MAX_SIMULTANEOUS_JOBS, MAX_JOB_HISTORY,
              MAX_TEMP_BYTES_PER_JOB, MAX_STORAGE_BYTES):
    if value < 1:
        raise RuntimeError("Service limits must be positive.")
if MIN_FREE_DISK_BYTES < 0 or MAX_JOB_HISTORY < MAX_SIMULTANEOUS_JOBS:
    raise RuntimeError("Invalid disk reserve or history limit.")

TERMINAL = {"completed", "error", "cancelled", "interrupted", "expired", "duplicate"}
JOBS = {}
TASKS = set()
PROCESSES = set()
STOPPING = False
ANALYSIS_BUSY = False
logger = logging.getLogger("uvicorn.error")


class UrlPayload(BaseModel):
    url: HttpUrl
    account: Literal["", "youtube", "auto"] | None = None


class DownloadPayload(UrlPayload):
    analysis_id: str | None = Field(default=None,max_length=64)
    format_id: str = Field(default="", max_length=200)
    quality: Literal[0, 2160, 1440, 1080, 720, 480, 360] = 0
    destination: Literal["transfer", "library"] = "library"
    members_only: bool = False
    enqueue: bool = False
    force: bool = False
    audio_only: bool = False
    filename: str = Field(default="download", max_length=500)


def dependencies_ready():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe")
                and importlib.util.find_spec("yt_dlp"))


def directory_bytes(directory):
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file() and not p.is_symlink()) if directory.exists() else 0


def storage_error(job_dir=None, admission=False):
    try:
        free = shutil.disk_usage(DOWNLOAD_DIR).free
        # Leave space for both input streams and FFmpeg's output before starting.
        if free < MIN_FREE_DISK_BYTES + (MAX_TEMP_BYTES_PER_JOB if admission else 0):
            return "Insufficient storage. Save/remove completed files or free server disk space."
        used = directory_bytes(DOWNLOAD_DIR)
        if used + (MAX_TEMP_BYTES_PER_JOB if admission else 0) > MAX_STORAGE_BYTES:
            return "Temporary storage limit reached. Remove completed server files and retry."
        if job_dir and directory_bytes(job_dir) > MAX_TEMP_BYTES_PER_JOB:
            return "Download exceeded its temporary storage limit."
    except OSError:
        return "Temporary storage is unavailable."
    return None


def public_job(job_id, job):
    result = {key: job.get(key) for key in (
        "status", "progress", "downloaded_bytes", "total_bytes", "speed", "eta", "filename",
        "requested_name", "error", "created_at", "expires_at", "destination", "library_id", "media_key", "batch_id", "metadata")}
    result["job_id"] = job_id
    if job["status"] == "completed":
        result["download_url"] = (f"/api/library/{job['library_id']}/file?download=1" if job.get('library_id') else f"/api/download/file/{job_id}")
    return result


def get_job(job_id):
    cleanup_stale_jobs()
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or job_id not in JOBS:
        raise HTTPException(404, "Download job expired or not found.")
    return JOBS[job_id]


def remove_job(job_id):
    job = JOBS.get(job_id)
    if job and job["status"] in TERMINAL and not job.get("leases"):
        shutil.rmtree(job["job_dir"], ignore_errors=True)
        JOBS.pop(job_id, None)
        if STORE:
            archived = dict(job, status="expired", file_path=None, job_dir=None, leases=0)
            STORE.put('job',job_id,archived)


def cleanup_stale_jobs():
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if job["status"] in TERMINAL and job.get("expires_at") and now >= job["expires_at"]:
            remove_job(job_id)


def cleanup_orphan_dirs():
    # Startup has no surviving workers. Periodic cleanup never touches active jobs.
    known = {Path(j["job_dir"]) for j in JOBS.values() if j.get("job_dir")}
    for child in DOWNLOAD_DIR.iterdir():
        if re.fullmatch(r"[a-f0-9]{32}", child.name) and child.is_dir() and not child.is_symlink() and child not in known:
            shutil.rmtree(child, ignore_errors=True)


def track(task):
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return task


async def stop_process(process):
    # Each worker starts a new process group; FFmpeg and downloader children join it.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
    # A child may outlive the parent, so kill the group even if the parent exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()
    PROCESSES.discard(process)


async def checked_url(url):
    try:
        return await asyncio.wait_for(asyncio.to_thread(validate_remote_url, url), timeout=10)
    except asyncio.TimeoutError as exc:
        raise HTTPException(400, "Host resolution timed out. Retry shortly.") from exc


async def run_worker(payload, timeout, job=None):
    # yt-dlp makes a short-lived SQLite read copy. Contain it in a private worker
    # directory and remove it even when that worker is cancelled or times out.
    session_area=tempfile.mkdtemp(prefix='browser-read-',dir=CONFIG_DIR) if payload.get('cookiebrowser') else None
    child_env=os.environ.copy()
    if session_area: child_env['TMPDIR']=session_area
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(BASE_DIR / "worker.py"), env=child_env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=1024*1024)
    except BaseException:
        if session_area: shutil.rmtree(session_area,ignore_errors=True)
        raise
    PROCESSES.add(process)
    result = None
    failure = None
    started = time.monotonic()
    read_task = None
    try:
        process.stdin.write(json.dumps(payload).encode() + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        read_task = asyncio.create_task(process.stdout.readline())
        while True:
            if STOPPING or (job and job.get("cancel_requested")):
                raise asyncio.CancelledError
            if time.monotonic() - started >= timeout:
                raise RuntimeError("Job timed out. Try a smaller file or retry later.")
            if job:
                reason = storage_error(Path(job["job_dir"]))
                if reason:
                    raise RuntimeError(reason)
            done, _ = await asyncio.wait({read_task}, timeout=0.25)
            if not done:
                continue
            line = read_task.result()
            if not line:
                break
            event = json.loads(line)
            if event["type"] == "progress" and job:
                if not job.get("cancel_requested"):
                    job.update(event["data"])
                    if STORE and time.monotonic() - job.get('_persisted_at',0) > 1:
                        persist_job(job)
            elif event["type"] == "result":
                result = event["data"]
            elif event["type"] == "error":
                failure = event["data"]
            read_task = asyncio.create_task(process.stdout.readline())
        await asyncio.wait_for(process.wait(), timeout=max(0.1, timeout - (time.monotonic() - started)))
        if failure or process.returncode or result is None:
            raise RuntimeError(failure or "Media worker failed. The source may be unavailable or protected.")
        return result
    finally:
        if read_task:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
        try:
            await stop_process(process)
        finally:
            if session_area: shutil.rmtree(session_area,ignore_errors=True)


async def run_download(job_id, payload):
    job = JOBS[job_id]
    try:
        result = await run_worker(payload, JOB_TIMEOUT_SECONDS, job)
        if job.get("cancel_requested"):
            raise asyncio.CancelledError
        output = Path(result["file_path"])
        if output.parent != Path(job["job_dir"]) or output.is_symlink() or output.suffix.lower() not in MEDIA_EXTENSIONS or not output.is_file():
            raise RuntimeError("No valid output file was produced.")
        job.update(status='processing')
        persist_job(job)
        validation = await probe(output)
        if job.get("cancel_requested"):
            raise asyncio.CancelledError
        if payload.get('audio_only') and not any(x['codec_type']=='audio' for x in validation['streams']):
            raise RuntimeError('Requested audio is missing from the completed file.')
        if payload.get('quality') and not payload.get('audio_only') and not payload.get('format_id'):
            height=max((x.get('height') or 0 for x in validation['streams']),default=0)
            if height>payload['quality']:
                raise RuntimeError('Downloaded resolution exceeds the requested quality ceiling.')
        size = output.stat().st_size
        if size > MAX_FILESIZE:
            raise RuntimeError("Completed file exceeds the configured maximum download size.")
        reason = storage_error(Path(job["job_dir"]))
        if reason:
            raise RuntimeError(reason)
        # Retain only the final file, not fragments or separate video/audio inputs.
        for child in output.parent.iterdir():
            if child != output:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        metadata = result.get('metadata') or job.get('metadata') or {}
        job['metadata'] = metadata
        if job['requested_name']=='download' and metadata.get('suggested_filename'):
            job['requested_name']=metadata['suggested_filename']
        actual_key = metadata.get('media_key') or job['media_key']
        job['media_key'] = actual_key
        duplicate = next((i for i,v in STORE.all('library').items() if v.get('media_key')==actual_key and v.get('status')=='completed'),None) if STORE else None
        if job.get('destination') == 'library' and duplicate and not payload.get('force'):
            job.update(status='duplicate', library_id=duplicate, error='This media is already in My Library.')
            return
        if job.get('destination') == 'library':
            job.update(status='saving'); persist_job(job)
            output = await publish_library(job,output,metadata,validation)
        if payload.get('account') == 'youtube' and STORE:
            accounts.verified_download(metadata,STORE)
        job.update(status="completed", progress=100.0, file_path=str(output), filename=output.name,
                   downloaded_bytes=size, total_bytes=size, speed=None, eta=0)
    except asyncio.CancelledError:
        job.update(status="cancelled", error=None, speed=None, eta=None)
    except Exception as exc:
        # Do not put source URLs, credentials or verbose yt-dlp stderr in logs.
        job.update(status="error", error=str(exc) or "Media worker timed out.", speed=None, eta=None)
        if payload.get('account')=='youtube' and STORE:
            accounts.record_failure(str(exc),STORE)
        logger.warning("Job %s failed: %s", job_id[:8], type(exc).__name__)
    finally:
        if job["status"] != "completed" or job.get("library_id"):
            shutil.rmtree(job["job_dir"], ignore_errors=True)
        job["expires_at"] = None if job.get('library_id') else time.time() + JOB_TTL_SECONDS
        persist_job(job)


async def cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        cleanup_stale_jobs()
        cleanup_orphan_dirs()


@asynccontextmanager
async def lifespan(_app):
    global STOPPING
    STOPPING = False
    global STORE, QUEUE_PAUSED
    ensure_storage()
    for session_area in CONFIG_DIR.glob('browser-read-*'):
        if session_area.is_dir() and not session_area.is_symlink():
            shutil.rmtree(session_area,ignore_errors=True)
    STORE = Store(STATE_DIR/'library.sqlite3')
    JOBS.clear()
    for job_id, job in STORE.all('job').items():
        job.update(job_id=job_id,leases=0,cancel_requested=False)
        if job['status'] not in TERMINAL:
            job.update(status='interrupted',error='Server restarted. Retry to start a fresh download.',expires_at=time.time()+JOB_TTL_SECONDS)
            STORE.put('job',job_id,job)
        if job.get('job_dir') and not job.get('library_id'):
            JOBS[job_id] = job
    await recover_library()
    QUEUE_PAUSED = bool((STORE.get('settings','owner') or {}).get('queue_paused'))
    cleanup_stale_jobs()
    cleanup_orphan_dirs()
    cleanup = asyncio.create_task(cleanup_loop())
    scheduler = asyncio.create_task(queue_loop())
    try:
        yield
    finally:
        STOPPING = True
        cleanup.cancel()
        scheduler.cancel()
        for job in JOBS.values():
            if job["status"] not in TERMINAL:
                job["cancel_requested"] = True
        for task in list(TASKS):
            task.cancel()
        await asyncio.gather(*list(TASKS), cleanup, scheduler, return_exceptions=True)
        await asyncio.gather(*(stop_process(p) for p in list(PROCESSES)), return_exceptions=True)
        STORE.close(); STORE = None


class LeasedFileResponse(FileResponse):
    def __init__(self, *args, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Release even on a disconnected browser or a failed streaming response.
            self.release()


app = FastAPI(title=APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
if os.getenv('OVD_ALLOWED_HOSTS'):
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=os.environ['OVD_ALLOWED_HOSTS'].split(','),www_redirect=False)
app.add_middleware(security.BodyLimit)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def browser_protection(request: Request, call_next):
    # Prevent another website from driving this private service from an authorized browser.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and (urlparse(origin).netloc != request.headers.get("host") or urlparse(origin).scheme != request.url.scheme)):
            return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
    if request.url.path.startswith('/api/') and request.url.path not in {'/api/health','/api/auth/status','/api/auth/login','/api/auth/setup'}:
        if not STORE or not (security.local_development(request) or security.authenticated(request,STORE,CONFIG_DIR)):
            return JSONResponse({'detail':'Owner login required.'},status_code=401,headers={'Cache-Control':'no-store'})
    if request.url.path.startswith('/api/') and request.url.path not in {'/api/health','/api/auth/status','/api/auth/login','/api/auth/setup','/api/auth/logout','/api/auth/mfa/start','/api/auth/mfa/confirm'}:
        if os.getenv('OVD_REQUIRE_MFA')=='1' and not security.mfa_ready(CONFIG_DIR):
            return JSONResponse({'detail':'Complete MFA enrollment before using the library.'},status_code=403)
    if request.url.path.startswith('/api/') and request.url.path!='/api/health' and os.getenv('OVD_REQUIRE_HTTPS')=='1' and request.url.scheme!='https':
        return JSONResponse({'detail':'HTTPS is required.'},status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def home():
    return (BASE_DIR / "templates/index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health():
    try:
        ensure_storage()
        ready = dependencies_ready() and not STOPPING and STORE is not None
    except (OSError, RuntimeError):
        ready = False
    return JSONResponse({"ok": ready, "name": APP_NAME}, status_code=200 if ready else 503)


@app.get("/api/jobs")
async def recent_jobs():
    cleanup_stale_jobs()
    stored = STORE.all('job') if STORE else {}
    stored.update(JOBS)
    return {"jobs": [public_job(k, v) for k, v in sorted(stored.items(),key=lambda kv:kv[1]['created_at'],reverse=True)[:200]],
            "free_bytes": shutil.disk_usage(DOWNLOAD_DIR).free,
            "storage_warning": storage_error(admission=True), "max_file_bytes": MAX_FILESIZE}


@app.post("/api/info")
async def get_info(payload: UrlPayload):
    global ANALYSIS_BUSY
    if STOPPING or not dependencies_ready():
        raise HTTPException(503, "FFmpeg or yt-dlp is unavailable. Ask the server owner to check the installation.")
    if ANALYSIS_BUSY:
        raise HTTPException(429, "Another link is being analyzed. Retry shortly.")
    ANALYSIS_BUSY = True
    task = None
    try:
        url = await checked_url(str(payload.url))
        account=accounts.resolve_account(url,payload.account,STORE,CONFIG_DIR)
        task = track(asyncio.create_task(run_worker({"mode":"info","url":url,"account":account,**accounts.worker_credentials(url,account,STORE,CONFIG_DIR)},INFO_TIMEOUT_SECONDS)))
        result=await task
        if account=='youtube':accounts.record_success(result,STORE)
        analysis_id=uuid.uuid4().hex
        connection=accounts.account_status(STORE,CONFIG_DIR) if account else {}
        cached={'metadata':result,'url':url,'account':account,'connection_id':connection.get('connection_id'),'created_at':time.time()}
        STORE.put('preview',stable_key(url),cached)
        STORE.put('analysis',analysis_id,cached)
        # Analysis tokens contain metadata only, are owner-protected and expire in 30 minutes.
        for old_id,old in STORE.all('analysis').items():
            if time.time()-old['created_at']>1800:STORE.delete('analysis',old_id)
        return result | {'analysis_id':analysis_id,'account':account}
    except (RuntimeError, asyncio.TimeoutError) as exc:
        if locals().get('account')=='youtube':accounts.record_failure(str(exc),STORE)
        raise HTTPException(422,str(exc) or "Link analysis timed out.") from exc
    except asyncio.CancelledError:
        raise HTTPException(503, "Server is shutting down.")
    finally:
        ANALYSIS_BUSY = False


@app.post("/api/download/start")
async def start_download(payload: DownloadPayload):
    if STOPPING or not dependencies_ready():
        raise HTTPException(503, "Server is not ready: check FFmpeg and yt-dlp.")
    url = await checked_url(str(payload.url))
    account=accounts.resolve_account(url,payload.account,STORE,CONFIG_DIR)
    accounts.cookie_path(url,account,STORE,CONFIG_DIR)
    if payload.analysis_id:
        analysis=STORE.get('analysis',payload.analysis_id)
        if not analysis or time.time()-analysis['created_at']>1800 or stable_key(analysis['url'])!=stable_key(url):
            raise HTTPException(409,'Analyze this URL again before downloading.')
        connection_id=accounts.account_status(STORE,CONFIG_DIR).get('connection_id') if account else None
        if analysis['account']!=account or analysis.get('connection_id')!=connection_id:
            raise HTTPException(409,'The account selection changed. Analyze again so analysis and download use the same connection.')
    format_id = payload.format_id.strip()
    if format_id and not re.fullmatch(r"[A-Za-z0-9_.+\-,:/]+", format_id):
        raise HTTPException(400, "Invalid format id.")
    cleanup_stale_jobs()
    key = stable_key(url)
    cached=STORE.get('preview',key)
    if cached and time.time()-cached['created_at']<300 and cached['account']==account:
        key=cached['metadata'].get('media_key') or key
    if not payload.force:
        for old_id, old in JOBS.items():
            if old.get('media_key') == key and (old['status'] not in TERMINAL or old.get('destination')==payload.destination and old['status']=='completed'):
                return {'job_id':old_id,'duplicate':True,'requested_name':old['requested_name']}
        for item_id,item in STORE.all('library').items():
            if item.get('media_key')==key and item.get('status')=='completed' and payload.destination=='library':
                return {'library_id':item_id,'duplicate':True,'requested_name':item['filename']}
    active = sum(j['status'] not in TERMINAL and j['status']!='queued' for j in JOBS.values())
    queued = sum(j['status']=='queued' for j in JOBS.values())
    if not payload.enqueue and active >= MAX_SIMULTANEOUS_JOBS:
        raise HTTPException(429, "The server is busy with another download. Wait or queue it.")
    if queued >= MAX_QUEUE:
        raise HTTPException(429, 'Queue is full.')
    ensure_storage()
    reason = storage_error(admission=True)
    if payload.destination=='library' and (directory_bytes(LIBRARY_DIR)+MAX_FILESIZE>MAX_LIBRARY_BYTES or shutil.disk_usage(LIBRARY_DIR).free<MIN_FREE_DISK_BYTES+MAX_TEMP_BYTES_PER_JOB+MAX_FILESIZE):
        reason = 'Library quota reached. Delete library items or increase the server quota.'
    if reason:
        raise HTTPException(507, reason)
    # Bound resident terminal jobs. Durable history remains in SQLite.
    while len(JOBS) >= MAX_JOB_HISTORY:
        candidate = next((i for i,j in JOBS.items() if j['status'] in TERMINAL and not j.get('leases')),None)
        if candidate is None:
            break
        if JOBS[candidate].get('library_id'):
            JOBS.pop(candidate)
        else:
            remove_job(candidate)
    job_id = uuid.uuid4().hex
    job_dir = DOWNLOAD_DIR/job_id; job_dir.mkdir(mode=0o700)
    requested_name = _sanitize_stem(payload.filename)
    worker_payload = dict(mode='download',url=url,format_id=format_id,audio_only=payload.audio_only,
        quality=payload.quality,account=account,force=payload.force,members_only=payload.members_only,
        requested_name=requested_name,job_dir=str(job_dir))
    job = dict(job_id=job_id,status='queued',progress=0,requested_name=requested_name,
        job_dir=str(job_dir),created_at=time.time(),cancel_requested=False,leases=0,
        destination=payload.destination,media_key=key,payload=worker_payload)
    JOBS[job_id] = job; persist_job(job)
    if not payload.enqueue and not QUEUE_PAUSED:
        launch(job_id)
    return {'job_id':job_id,'requested_name':requested_name}


@app.get("/api/download/progress/{job_id}")
async def download_progress(job_id: str):
    job = JOBS.get(job_id) or STORE.get('job',job_id)
    if not job:
        raise HTTPException(404,'Job not found.')
    cleanup_stale_jobs()
    return public_job(job_id, JOBS.get(job_id) or STORE.get('job',job_id))


@app.post("/api/download/cancel/{job_id}")
async def cancel_download(job_id: str):
    job = get_job(job_id)
    if job['status']=='queued':
        job.update(status='cancelled',expires_at=time.time()+JOB_TTL_SECONDS)
        shutil.rmtree(job['job_dir'],ignore_errors=True)
        persist_job(job)
    elif job["status"] not in TERMINAL:
        job.update(cancel_requested=True, status="cancelling", speed=None, eta=None)
        persist_job(job)
    return public_job(job_id, job)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = JOBS.get(job_id) or STORE.get('job',job_id)
    if not job: raise HTTPException(404,'Job not found.')
    if job["status"] not in TERMINAL or job.get("leases"):
        raise HTTPException(409, "Cancel the active job or wait for the file transfer to finish first.")
    if job.get('library_id'):
        JOBS.pop(job_id,None)
    else:
        remove_job(job_id)
    STORE.delete('job',job_id)
    return {"ok": True}


@app.get("/api/download/file/{job_id}")
async def download_file(job_id: str, name: str | None = None):
    job = get_job(job_id)
    if job["status"] != "completed":
        raise HTTPException(404, "Download is not ready.")
    if job.get('library_id'):
        return await library_file(job['library_id'],download=True,name=name)
    output = Path(job["file_path"])
    if not output.is_file():
        remove_job(job_id)
        raise HTTPException(404, "Download file expired or not found.")
    save_name = f"{_sanitize_stem(name, fallback=output.stem)}{output.suffix}"
    job["leases"] += 1
    def release():
        job["leases"] = max(0, job["leases"] - 1)
    return LeasedFileResponse(output, filename=save_name, media_type="application/octet-stream", release=release)


def ensure_storage():
    roots = [DOWNLOAD_DIR, LIBRARY_DIR, STATE_DIR, CONFIG_DIR]
    for directory in roots:
        if directory.is_symlink():
            raise RuntimeError('Storage root must not be a symlink.')
        if os.getenv('LIBRARY_REQUIRE_MOUNT') == '1' and directory == LIBRARY_DIR and not directory.is_mount():
            raise RuntimeError('Library volume is missing. Refusing to write to the server root disk.')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory,0o700)
        if not os.access(directory,os.W_OK):
            raise RuntimeError('Storage is not writable.')
    if any(a==b or a in b.parents or b in a.parents for i,a in enumerate(roots) for b in roots[i+1:]):
        raise RuntimeError('Temporary, library, state and config storage must be separate directories.')


def persist_job(job):
    if STORE:
        STORE.put('job',job['job_id'],job)
        job['_persisted_at'] = time.monotonic()


def launch(job_id):
    global LAST_START
    job = JOBS[job_id]
    try:
        ensure_storage()
        reason=storage_error(admission=True)
        if reason: raise HTTPException(507,reason)
        if job.get('destination')=='library' and (directory_bytes(LIBRARY_DIR)+MAX_FILESIZE>MAX_LIBRARY_BYTES or shutil.disk_usage(LIBRARY_DIR).free<MIN_FREE_DISK_BYTES+MAX_TEMP_BYTES_PER_JOB+MAX_FILESIZE):
            raise HTTPException(507,'Insufficient library capacity for this queued job.')
        payload=dict(job['payload'])
        payload.update(accounts.worker_credentials(payload['url'],payload.get('account',''),STORE,CONFIG_DIR))
    except (HTTPException,OSError,RuntimeError) as exc:
        job.update(status='error',error=exc.detail if isinstance(exc,HTTPException) else 'Server storage is unavailable.',expires_at=time.time()+JOB_TTL_SECONDS)
        persist_job(job); shutil.rmtree(job['job_dir'],ignore_errors=True)
        return
    job.update(status='preparing'); persist_job(job)
    LAST_START=time.monotonic()
    track(asyncio.create_task(run_download(job_id,payload)))


async def queue_loop():
    while True:
        await asyncio.sleep(.25)
        if QUEUE_PAUSED or STOPPING:
            continue
        active=sum(j['status'] not in TERMINAL|{'queued'} for j in JOBS.values())
        # Accounts use at least ten seconds between downloads; public jobs are still sequential by default.
        for job_id,job in JOBS.items():
            if active>=MAX_SIMULTANEOUS_JOBS: break
            if job['status']=='queued':
                if job['payload'].get('account') and (ANALYSIS_BUSY or time.monotonic()-LAST_START < 10): break
                launch(job_id); active+=1


async def publish_library(job, output, metadata, validation):
    ensure_storage()
    size=output.stat().st_size
    if directory_bytes(LIBRARY_DIR)+size>MAX_LIBRARY_BYTES or shutil.disk_usage(LIBRARY_DIR).free<size+MIN_FREE_DISK_BYTES:
        raise RuntimeError('Insufficient library storage.')
    item_id=uuid.uuid4().hex; directory=LIBRARY_DIR/item_id; directory.mkdir(mode=0o700)
    target=directory/output.name; partial=directory/'saving.partial'
    item=dict(library_id=item_id,status='saving',job_id=job['job_id'],media_key=job['media_key'],
        filename=output.name,path=str(target),metadata=metadata,validation=validation,size=size,
        saved_at=time.time(),collection='',position=0,watched=False)
    STORE.put('library',item_id,item)
    try:
        # Copy on the SERVER, bounded chunks. The final name is published atomically.
        with output.open('rb') as src, partial.open('xb') as dst:
            while chunk:=src.read(1024*1024):
                if STOPPING or job.get('cancel_requested'): raise asyncio.CancelledError
                if shutil.disk_usage(directory).free<MIN_FREE_DISK_BYTES+len(chunk):
                    raise RuntimeError('Insufficient library storage during save.')
                dst.write(chunk)
                await asyncio.sleep(0)
            dst.flush(); os.fsync(dst.fileno())
        partial.rename(target)
        descriptor=os.open(directory,os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        item['status']='completed'; STORE.put('library',item_id,item)
        job['library_id']=item_id; persist_job(job)
        return target
    except BaseException:
        # Only this uncommitted save is removed. Existing library items are never cleaned by TTL.
        shutil.rmtree(directory,ignore_errors=True); STORE.delete('library',item_id)
        raise


async def recover_library():
    # Complete a crash after atomic file publication but before SQLite completion.
    for item_id,item in STORE.all('library').items():
        if item.get('status')=='completed':
            job=JOBS.get(item['job_id']) or STORE.get('job',item['job_id'])
            if job and job['status'] not in {'completed','expired'}:
                job.update(status='completed',library_id=item_id,file_path=item['path'],filename=item['filename'],expires_at=None,progress=100,error=None)
                STORE.put('job',item['job_id'],job)
            continue
        if item.get('status')!='saving': continue
        target=Path(item['path'])
        try:
            if target.parent != LIBRARY_DIR/item_id or not target.is_file() or target.is_symlink():
                raise RuntimeError('Save was interrupted before publication.')
            item['validation']=await probe(target); item['status']='completed'
            STORE.put('library',item_id,item)
            job=JOBS.get(item['job_id']) or STORE.get('job',item['job_id'])
            if job:
                job.update(status='completed',library_id=item_id,file_path=str(target),filename=item['filename'],expires_at=None,progress=100,error=None)
                STORE.put('job',item['job_id'],job)
        except RuntimeError:
            item.update(status='incomplete')
            STORE.put('library',item_id,item)
            # Keep incomplete data for explicit owner deletion; no automatic library cleanup.


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, _exception):
    # Default validation errors can echo a cookie file or password back to the browser.
    return JSONResponse({'detail':'Invalid request fields.'},status_code=422)


class LoginPayload(BaseModel):
    password: str = Field(min_length=1,max_length=256)
    code: str = Field(default='',max_length=6)


@app.get('/api/auth/status')
async def auth_status(request: Request):
    return {'configured':bool(security.owner_hash(CONFIG_DIR)), 'setup_available':security.loopback_setup(request) and not security.owner_hash(CONFIG_DIR), 'mfa_enabled':security.mfa_ready(CONFIG_DIR), 'mfa_required':os.getenv('OVD_REQUIRE_MFA')=='1',
            'authenticated':bool(STORE and (security.local_development(request) or security.authenticated(request,STORE,CONFIG_DIR)))}


@app.post('/api/auth/login')
async def owner_login(payload: LoginPayload):
    token=await security.login(payload.password,STORE,CONFIG_DIR,payload.code)
    response=JSONResponse({'ok':True})
    response.set_cookie('ovd_session',token,max_age=security.SESSION_SECONDS,httponly=True,
        secure=os.getenv('COOKIE_SECURE','1')=='1',samesite='strict',path='/')
    return response


@app.post('/api/auth/setup')
async def owner_setup(payload: LoginPayload, request: Request):
    if not security.loopback_setup(request) or security.owner_hash(CONFIG_DIR):
        raise HTTPException(403,'Owner setup is unavailable.')
    security.rate_limit(STORE)
    if len(payload.password)<12:
        raise HTTPException(400,'Use at least 12 characters.')
    digest=await asyncio.to_thread(security.password_hash,payload.password)
    # Recheck after hashing: concurrent bootstrap requests cannot replace the owner.
    if security.owner_hash(CONFIG_DIR):
        raise HTTPException(409,'Owner is already configured.')
    security.write_owner(CONFIG_DIR,{'password_hash':digest})
    return {'ok':True}


@app.post('/api/auth/mfa/start')
async def mfa_start(payload: LoginPayload):
    security.rate_limit(STORE)
    await security.check_password(payload.password,CONFIG_DIR)
    data=security.owner_config(CONFIG_DIR)
    if data.get('totp_secret'):
        raise HTTPException(409,'MFA is already enabled. Recovery uses the private server CLI.')
    secret=security.base64.b32encode(os.urandom(20)).decode()
    data['totp_pending']=secret; data['totp_pending_expires']=time.time()+600
    security.write_owner(CONFIG_DIR,data)
    return {'secret':secret,'uri':'otpauth://totp/OpenVideoDownloader:Owner?secret='+secret+'&issuer=OpenVideoDownloader&algorithm=SHA1&digits=6&period=30'}


@app.post('/api/auth/mfa/confirm')
async def mfa_confirm(payload: LoginPayload):
    security.rate_limit(STORE)
    await security.check_password(payload.password,CONFIG_DIR)
    data=security.owner_config(CONFIG_DIR)
    if data.get('totp_secret') or data.get('totp_pending_expires',0)<time.time() or not security.verify_totp(data,payload.code,'totp_pending'):
        raise HTTPException(400,'Enrollment expired or code incorrect. Start enrollment again.')
    data['totp_secret']=data.pop('totp_pending'); data.pop('totp_pending_expires',None)
    security.write_owner(CONFIG_DIR,data)
    with STORE.db: STORE.db.execute('DELETE FROM sessions')
    response=JSONResponse({'ok':True}); response.delete_cookie('ovd_session',path='/')
    return response


@app.post('/api/auth/logout')
async def owner_logout(request: Request):
    with STORE.db:
        STORE.db.execute('DELETE FROM sessions WHERE hash=?',(security.session_key(request.cookies.get('ovd_session',''),CONFIG_DIR),))
    response=JSONResponse({'ok':True}); response.delete_cookie('ovd_session',path='/')
    return response


class BatchPayload(BaseModel):
    items: list[DownloadPayload] = Field(min_length=1,max_length=100)


@app.post('/api/batch')
async def batch_download(payload: BatchPayload):
    batch_id=uuid.uuid4().hex; results=[]
    for index,item in enumerate(payload.items):
        try:
            result=await start_download(item.model_copy(update={'enqueue':True}))
            if result.get('job_id') and not result.get('duplicate'):
                job=JOBS[result['job_id']]; job['batch_id']=batch_id; persist_job(job)
            results.append(dict(index=index,**result))
        except HTTPException as exc:
            results.append(dict(index=index,error=exc.detail))
    return {'batch_id':batch_id,'results':results}


class QueuePayload(BaseModel):
    paused: bool


@app.post('/api/queue')
async def queue_control(payload: QueuePayload):
    global QUEUE_PAUSED
    QUEUE_PAUSED=payload.paused
    settings=STORE.get('settings','owner') or {}; settings['queue_paused']=QUEUE_PAUSED
    STORE.put('settings','owner',settings)
    return {'paused':QUEUE_PAUSED}


@app.post('/api/jobs/{job_id}/retry')
async def retry_job(job_id: str):
    old=JOBS.get(job_id) or STORE.get('job',job_id)
    if not old or old['status'] not in {'error','cancelled','interrupted','expired'}:
        raise HTTPException(409,'Only failed, cancelled, interrupted or expired jobs can be retried.')
    payload=old.get('payload') or {}
    return await start_download(DownloadPayload(url=payload['url'],format_id=payload.get('format_id',''),
        quality=payload.get('quality',0),audio_only=payload.get('audio_only',False),account=payload.get('account',''),
        filename=old['requested_name'],destination=old.get('destination','library'),members_only=payload.get('members_only',False),enqueue=True))


def get_library(item_id):
    item=STORE.get('library',item_id)
    if not item or not re.fullmatch(r'[a-f0-9]{32}',item_id):
        raise HTTPException(404,'Library item not found.')
    return item


def public_library(item):
    result={k:v for k,v in item.items() if k not in {'path','job_id'}}
    result['play_url']=f"/api/library/{item['library_id']}/file"
    result['download_url']=result['play_url']+'?download=1'
    return result


@app.get('/api/library')
async def library_list(q: str='',sort: Literal['newest','oldest','title','size']='newest',collection: str='',platform: str='',creator: str='',watched: bool|None=None):
    items=list(STORE.all('library').values())
    def matches(item):
        meta=item.get('metadata') or {}
        return (not q or q.casefold() in (str(meta.get('title',''))+' '+str(meta.get('uploader',''))+' '+item['filename']).casefold()) and (not collection or item.get('collection')==collection) and (not platform or meta.get('platform')==platform) and (not creator or meta.get('uploader')==creator) and (watched is None or item.get('watched',False)==watched)
    items=[i for i in items if matches(i)]
    key={'newest':lambda i:-i['saved_at'],'oldest':lambda i:i['saved_at'],'title':lambda i:(i.get('metadata') or {}).get('title',i['filename']).casefold(),'size':lambda i:-i['size']}[sort]
    items.sort(key=key)
    return {'items':[public_library(i) for i in items]}


@app.get('/api/library/{item_id}/file')
@app.head('/api/library/{item_id}/file')
async def library_file(item_id: str,download: bool=False,name: str|None=None):
    ensure_storage()
    item=get_library(item_id); path=Path(item['path'])
    if item['status']!='completed' or path.parent != LIBRARY_DIR/item_id or path.is_symlink() or not path.is_file():
        raise HTTPException(404,'Library media is unavailable. Check its storage volume.')
    LIBRARY_LEASES[item_id]=LIBRARY_LEASES.get(item_id,0)+1
    def release(): LIBRARY_LEASES[item_id]=max(0,LIBRARY_LEASES.get(item_id,0)-1)
    save_name=_sanitize_stem(name,fallback=path.stem)+path.suffix
    return LeasedFileResponse(path,filename=save_name if download else None,
        media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',release=release,
        content_disposition_type='attachment' if download else 'inline')


class LibraryUpdate(BaseModel):
    position: float|None = Field(default=None,ge=0,le=86400*100)
    watched: bool|None = None
    collection: str|None = Field(default=None,max_length=100)


@app.patch('/api/library/{item_id}')
async def update_library(item_id: str,payload: LibraryUpdate):
    item=get_library(item_id)
    values=payload.model_dump(exclude_none=True)
    if 'position' in values:
        values['position']=min(values['position'],item.get('validation',{}).get('duration',values['position']))
    item.update(values); STORE.put('library',item_id,item)
    return public_library(item)


@app.delete('/api/library/{item_id}')
async def delete_library(item_id: str,confirm: bool=False):
    if not confirm: raise HTTPException(409,'Confirm permanent library deletion.')
    item=get_library(item_id)
    if LIBRARY_LEASES.get(item_id): raise HTTPException(409,'Media is currently being served. Retry after the transfer.')
    directory=LIBRARY_DIR/item_id
    if directory.is_symlink(): raise HTTPException(409,'Unsafe media directory.')
    shutil.rmtree(directory) if directory.exists() else None
    STORE.delete('library',item_id)
    for job_id,job in STORE.all('job').items():
        if job.get('library_id')==item_id:
            job.update(status='expired',file_path=None,library_id=None,expires_at=time.time())
            STORE.put('job',job_id,job)
            if job_id in JOBS: JOBS[job_id].update(job)
    return {'ok':True}


@app.get('/api/storage')
async def storage_dashboard():
    ensure_storage()
    return {'temporary_bytes':directory_bytes(DOWNLOAD_DIR),'library_bytes':directory_bytes(LIBRARY_DIR),
        'temporary_limit':MAX_STORAGE_BYTES,'library_limit':MAX_LIBRARY_BYTES,
        'free_bytes':min(shutil.disk_usage(DOWNLOAD_DIR).free,shutil.disk_usage(LIBRARY_DIR).free),
        'minimum_free_bytes':MIN_FREE_DISK_BYTES,'library_persistent':True,'queue_paused':QUEUE_PAUSED}


class Preferences(BaseModel):
    destination: Literal['library','transfer']='library'
    quality: Literal[0,2160,1440,1080,720,480,360]=0
    account_mode: Literal['auto','public','youtube']='auto'


@app.get('/api/settings')
async def get_settings():
    return {'destination':'library','quality':0,'account_mode':'auto'} | (STORE.get('settings','owner') or {})


@app.put('/api/settings')
async def save_settings(payload: Preferences):
    value=STORE.get('settings','owner') or {}; value.update(payload.model_dump(exclude_unset=True))
    STORE.put('settings','owner',value); return value


class CookiePayload(BaseModel):
    cookies: str = Field(min_length=1,max_length=200000)
    authorized: bool


@app.get('/api/accounts')
async def connected_accounts():
    return {'accounts':[accounts.account_status(STORE,CONFIG_DIR)],'supported':['youtube'],'local_chrome_available':accounts.local_chrome_available()}


@app.get('/api/accounts/youtube/profiles')
async def local_profiles(request: Request):
    if not security.loopback_setup(request):raise HTTPException(403,'Chrome profiles are available only in the local Mac app.')
    return {'profiles':accounts.chrome_profiles()}


class BrowserVerificationPayload(BaseModel):
    profile: str = Field(default='Default',max_length=30)
    url: HttpUrl


@app.post('/api/accounts/youtube/browser/verify')
async def verify_browser_connection(payload: BrowserVerificationPayload,request: Request):
    global ANALYSIS_BUSY
    if not security.loopback_setup(request):raise HTTPException(403,'Use the local Mac app on 127.0.0.1:8001.')
    if ANALYSIS_BUSY or any(j['status'] not in TERMINAL and j.get('payload',{}).get('account')=='youtube' for j in JOBS.values()):
        raise HTTPException(409,'Wait for account activity before verifying a connection.')
    url=await checked_url(str(payload.url))
    parsed=urlparse(url)
    from urllib.parse import parse_qs
    video_id=parse_qs(parsed.query).get('v',[''])[0] if parsed.path=='/watch' else parsed.path.strip('/').split('/')[-1]
    if not accounts.youtube_url(url) or parsed.scheme!='https' or not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id) or not (parsed.path=='/watch' or parsed.hostname=='youtu.be' or parsed.path.startswith(('/shorts/','/live/'))):
        raise HTTPException(400,'Provide one HTTPS YouTube video you can already watch, not a channel or playlist.')
    if (CONFIG_DIR/'youtube.cookies.txt').exists():raise HTTPException(409,'Disconnect the imported session before selecting Chrome.')
    candidate=accounts.chrome_connection(payload.profile)
    previous=STORE.get('account','youtube')
    ANALYSIS_BUSY=True
    try:
        info=await track(asyncio.create_task(run_worker({'mode':'info','url':url,'account':'youtube','cookiebrowser':'chrome','cookieprofile':payload.profile},INFO_TIMEOUT_SECONDS)))
        if info.get('availability')!='subscriber_only' or not info.get('channel_id') or not info.get('formats'):
            raise HTTPException(422,'This video did not confirm membership access. Provide one members-only video you can already watch. Your saved connection was kept.')
        if previous and previous.get('source')=='browser' and previous.get('profile')==payload.profile:
            candidate=previous
        STORE.put('account','youtube',candidate)
        accounts.record_success(info,STORE)
        return {'account':accounts.account_status(STORE,CONFIG_DIR),'message':'YouTube session and membership access confirmed. This profile is saved for automatic reuse.'}
    except (RuntimeError,asyncio.TimeoutError) as exc:
        if previous and previous.get('source')=='browser' and previous.get('profile')==payload.profile:accounts.record_failure(str(exc),STORE)
        raise HTTPException(422,str(exc) or 'Connection verification timed out. Your saved connection was kept.') from exc
    finally:ANALYSIS_BUSY=False


@app.post('/api/accounts/youtube')
async def connect_youtube(payload: CookiePayload):
    if not payload.authorized: raise HTTPException(400,'Confirm that this session is yours and you have access.')
    if any(j['status'] not in TERMINAL and j.get('payload',{}).get('account')=='youtube' for j in JOBS.values()) or ANALYSIS_BUSY:
        raise HTTPException(409,'Wait for account jobs and analysis to finish before replacing a session.')
    return accounts.import_cookies(payload.cookies,CONFIG_DIR,STORE)


class BrowserConnectionPayload(BaseModel):
    profile: str = Field(default='Default',max_length=30)


@app.post('/api/accounts/youtube/browser')
async def connect_local_browser(payload: BrowserConnectionPayload, request: Request):
    if not security.loopback_setup(request):
        raise HTTPException(403,'Use the local Mac preview on 127.0.0.1:8001.')
    if ANALYSIS_BUSY or any(j['status'] not in TERMINAL and j.get('payload',{}).get('account')=='youtube' for j in JOBS.values()):
        raise HTTPException(409,'Wait for account activity before changing the connection.')
    if (CONFIG_DIR/'youtube.cookies.txt').exists():
        raise HTTPException(409,'Disconnect the imported session before selecting Chrome.')
    return accounts.connect_chrome(payload.profile,STORE)


@app.delete('/api/accounts/youtube')
async def disconnect_youtube():
    if any(j['status'] not in TERMINAL and j.get('payload',{}).get('account')=='youtube' for j in JOBS.values()) or ANALYSIS_BUSY:
        raise HTTPException(409,'Cancel account jobs and wait for analysis before disconnecting.')
    (CONFIG_DIR/'youtube.cookies.txt').unlink(missing_ok=True)
    STORE.delete('account','youtube')
    return {'ok':True}


class ScanPayload(UrlPayload):
    account: Literal['youtube']='youtube'
    limit: int = Field(default=50,ge=1,le=200)


@app.post('/api/membership/scan')
async def scan_membership(payload: ScanPayload):
    global ANALYSIS_BUSY
    url=await checked_url(str(payload.url))
    parsed=urlparse(url)
    if not accounts.youtube_url(url) or not re.fullmatch(r'/(?:@[^/]+|channel/UC[A-Za-z0-9_-]{22})/membership/?',parsed.path) or parsed.query:
        raise HTTPException(400,'Use a YouTube channel membership tab URL ending in /membership.')
    status=accounts.account_status(STORE,CONFIG_DIR)
    if not status.get('verified_channels'):
        raise HTTPException(409,'First complete one accessible members-only download with this connection.')
    if ANALYSIS_BUSY: raise HTTPException(429,'Another analysis is running.')
    if any(j['status'] not in TERMINAL and j.get('payload',{}).get('account') for j in JOBS.values()):
        raise HTTPException(409,'Wait for account downloads before scanning.')
    credentials=accounts.worker_credentials(url,'youtube',STORE,CONFIG_DIR)
    ANALYSIS_BUSY=True
    try:
        result=await track(asyncio.create_task(run_worker(dict(mode='scan',url=url,**credentials,limit=payload.limit,verified_channels=status['verified_channels']),180)))
        manifest_id=stable_key(url)
        existing=STORE.get('manifest',manifest_id) or {'manifest_id':manifest_id,'source':url,'entries':{}}
        for entry in result['entries']:
            previous=existing['entries'].get(entry['media_key'],{})
            existing['entries'][entry['media_key']]=entry|{k:previous[k] for k in ('job_id','status') if k in previous}
        existing.update(scanned_at=time.time(),summary={k:v for k,v in result.items() if k!='entries'})
        STORE.put('manifest',manifest_id,existing)
        return existing
    except RuntimeError as exc:
        raise HTTPException(422,str(exc)) from exc
    finally:
        ANALYSIS_BUSY=False


@app.get('/api/membership')
async def manifests():
    records=STORE.all('manifest')
    saved_keys={i.get('media_key') for i in STORE.all('library').values() if i.get('status')=='completed'}
    for record in records.values():
        for entry in record['entries'].values():
            job=STORE.get('job',entry.get('job_id',''))
            if job: entry['status']=job['status']
            if entry['media_key'] in saved_keys: entry['status']='completed'
    return {'manifests':list(records.values())}


class ImportPayload(BaseModel):
    manifest_id: str = Field(max_length=100)
    media_keys: list[str] = Field(min_length=1,max_length=100)
    quality: Literal[0,2160,1440,1080,720,480,360]=0


@app.post('/api/membership/import')
async def import_membership(payload: ImportPayload):
    manifest=STORE.get('manifest',payload.manifest_id)
    if not manifest: raise HTTPException(404,'Manifest not found.')
    connection=accounts.account_status(STORE,CONFIG_DIR)
    if connection['status']!='verified' or not connection.get('verified_channels'):
        raise HTTPException(409,'Verify the account again before membership import.')
    results=[]
    for key in dict.fromkeys(payload.media_keys):
        entry=manifest['entries'].get(key)
        if not entry or entry['availability']!='subscriber_only':
            results.append({'media_key':key,'error':'Not a confirmed members-only item.'}); continue
        try:
            result=await start_download(DownloadPayload(url=entry['url'],account='youtube',destination='library',quality=payload.quality,filename=entry['title'],members_only=True,enqueue=True))
            if result.get('job_id'): entry.update(job_id=result['job_id'],status='queued')
            if result.get('library_id'): entry.update(status='completed')
            results.append({'media_key':key,**result})
        except HTTPException as exc:
            results.append({'media_key':key,'error':exc.detail})
    STORE.put('manifest',payload.manifest_id,manifest)
    return {'results':results}
