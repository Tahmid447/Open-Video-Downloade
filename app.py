"""Single-user service. Run exactly one Uvicorn worker (jobs are in memory)."""
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
import time
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from common import MAX_FILESIZE, MEDIA_EXTENSIONS, _sanitize_stem, validate_remote_url

BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "Open Video Downloader"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(Path(tempfile.gettempdir()) / "ovd-cloud"))).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
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

TERMINAL = {"completed", "error", "cancelled"}
JOBS = {}
TASKS = set()
PROCESSES = set()
STOPPING = False
ANALYSIS_BUSY = False
logger = logging.getLogger("uvicorn.error")


class UrlPayload(BaseModel):
    url: HttpUrl


class DownloadPayload(UrlPayload):
    format_id: str = Field(default="", max_length=200)
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
        "requested_name", "error", "created_at", "expires_at")}
    result["job_id"] = job_id
    if job["status"] == "completed":
        result["download_url"] = f"/api/download/file/{job_id}"
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


def cleanup_stale_jobs():
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if job["status"] in TERMINAL and now >= job.get("expires_at", float("inf")):
            remove_job(job_id)


def cleanup_orphan_dirs():
    # Startup has no surviving workers. Periodic cleanup never touches active jobs.
    known = {Path(j["job_dir"]) for j in JOBS.values()}
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
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", str(BASE_DIR / "worker.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=1024*1024)
    PROCESSES.add(process)
    process.stdin.write(json.dumps(payload).encode() + b"\n")
    await process.stdin.drain()
    process.stdin.close()
    result = None
    failure = None
    started = time.monotonic()
    read_task = asyncio.create_task(process.stdout.readline())
    try:
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
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        await stop_process(process)


async def run_download(job_id, payload):
    job = JOBS[job_id]
    try:
        result = await run_worker(payload, JOB_TIMEOUT_SECONDS, job)
        if job.get("cancel_requested"):
            raise asyncio.CancelledError
        output = Path(result["file_path"])
        if output.parent != Path(job["job_dir"]) or output.is_symlink() or output.suffix.lower() not in MEDIA_EXTENSIONS or not output.is_file():
            raise RuntimeError("No valid output file was produced.")
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
        job.update(status="completed", progress=100.0, file_path=str(output), filename=output.name,
                   downloaded_bytes=size, total_bytes=size, speed=None, eta=0)
    except asyncio.CancelledError:
        job.update(status="cancelled", error=None, speed=None, eta=None)
    except Exception as exc:
        # Do not put source URLs, credentials or verbose yt-dlp stderr in logs.
        job.update(status="error", error=str(exc) or "Media worker timed out.", speed=None, eta=None)
        logger.warning("Job %s failed: %s", job_id[:8], type(exc).__name__)
    finally:
        if job["status"] != "completed":
            shutil.rmtree(job["job_dir"], ignore_errors=True)
        job["expires_at"] = time.time() + JOB_TTL_SECONDS


async def cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        cleanup_stale_jobs()
        cleanup_orphan_dirs()


@asynccontextmanager
async def lifespan(_app):
    global STOPPING
    STOPPING = False
    cleanup_orphan_dirs()
    cleanup = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        STOPPING = True
        cleanup.cancel()
        for job in JOBS.values():
            if job["status"] not in TERMINAL:
                job["cancel_requested"] = True
        for task in list(TASKS):
            task.cancel()
        await asyncio.gather(*list(TASKS), cleanup, return_exceptions=True)
        await asyncio.gather(*(stop_process(p) for p in list(PROCESSES)), return_exceptions=True)


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


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def browser_protection(request: Request, call_next):
    # Prevent another website from driving this private service from an authorized browser.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlparse(origin).netloc != request.headers.get("host")):
            return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
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
    ready = dependencies_ready() and not STOPPING and os.access(DOWNLOAD_DIR, os.W_OK)
    return JSONResponse({"ok": ready, "name": APP_NAME}, status_code=200 if ready else 503)


@app.get("/api/jobs")
async def recent_jobs():
    cleanup_stale_jobs()
    return {"jobs": [public_job(k, v) for k, v in reversed(list(JOBS.items()))],
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
        task = track(asyncio.create_task(run_worker({"mode": "info", "url": url}, INFO_TIMEOUT_SECONDS)))
        return await task
    except (RuntimeError, asyncio.TimeoutError) as exc:
        raise HTTPException(422, str(exc) or "Link analysis timed out.") from exc
    except asyncio.CancelledError:
        raise HTTPException(503, "Server is shutting down.")
    finally:
        ANALYSIS_BUSY = False


@app.post("/api/download/start")
async def start_download(payload: DownloadPayload):
    if STOPPING or not dependencies_ready():
        raise HTTPException(503, "Server is not ready: check FFmpeg and yt-dlp.")
    url = await checked_url(str(payload.url))
    format_id = payload.format_id.strip()
    if format_id and not re.fullmatch(r"[A-Za-z0-9_.+\-,:/]+", format_id):
        raise HTTPException(400, "Invalid format id.")
    cleanup_stale_jobs()
    # Admission and job insertion are synchronous on the single event loop.
    if STOPPING:
        raise HTTPException(503, "Server is shutting down.")
    if sum(j["status"] not in TERMINAL for j in JOBS.values()) >= MAX_SIMULTANEOUS_JOBS:
        raise HTTPException(429, "The server is busy with another download. Wait or cancel it first.")
    reason = storage_error(admission=True)
    if reason:
        raise HTTPException(507, reason)
    if len(JOBS) >= MAX_JOB_HISTORY:
        for old_id in list(JOBS):
            remove_job(old_id)
            if len(JOBS) < MAX_JOB_HISTORY:
                break
        if len(JOBS) >= MAX_JOB_HISTORY:
            raise HTTPException(429, "History is busy serving files. Retry shortly.")
    job_id = uuid.uuid4().hex
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(mode=0o700)
    requested_name = _sanitize_stem(payload.filename)
    job = dict(status="preparing", progress=0, requested_name=requested_name,
               job_dir=str(job_dir), created_at=time.time(), cancel_requested=False, leases=0)
    JOBS[job_id] = job
    track(asyncio.create_task(run_download(job_id, dict(mode="download", url=url,
          format_id=format_id, audio_only=payload.audio_only, requested_name=requested_name, job_dir=str(job_dir)))))
    return {"job_id": job_id, "requested_name": requested_name}


@app.get("/api/download/progress/{job_id}")
async def download_progress(job_id: str):
    return public_job(job_id, get_job(job_id))


@app.post("/api/download/cancel/{job_id}")
async def cancel_download(job_id: str):
    job = get_job(job_id)
    if job["status"] not in TERMINAL:
        job.update(cancel_requested=True, status="cancelling", speed=None, eta=None)
    return public_job(job_id, job)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = get_job(job_id)
    if job["status"] not in TERMINAL or job.get("leases"):
        raise HTTPException(409, "Cancel the active job or wait for the file transfer to finish first.")
    remove_job(job_id)
    return {"ok": True}


@app.get("/api/download/file/{job_id}")
async def download_file(job_id: str, name: str | None = None):
    job = get_job(job_id)
    if job["status"] != "completed":
        raise HTTPException(404, "Download is not ready.")
    output = Path(job["file_path"])
    if not output.is_file():
        remove_job(job_id)
        raise HTTPException(404, "Download file expired or not found.")
    save_name = f"{_sanitize_stem(name, fallback=output.stem)}{output.suffix}"
    job["leases"] += 1
    def release():
        job["leases"] = max(0, job["leases"] - 1)
    return LeasedFileResponse(output, filename=save_name, media_type="application/octet-stream", release=release)
