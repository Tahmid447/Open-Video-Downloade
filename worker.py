"""Short-lived yt-dlp worker; stdout is bounded JSON events, never console logs."""
import json
from pathlib import Path
import resource
import sys
import time
import yt_dlp
from common import MAX_FILESIZE, MEDIA_EXTENSIONS, _progress_percent, _sanitize_stem, clean_formats, ydl_base_opts


def emit(kind, data):
    print(json.dumps({"type": kind, "data": data}), flush=True)


class QuietLogger:
    def debug(self, _message): pass
    def warning(self, _message): pass
    def error(self, _message): pass


def execute(payload):
    from network_guard import install
    install()
    # This limit is inherited by FFmpeg and also bounds unknown-size sources.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILESIZE, MAX_FILESIZE))
    opts = ydl_base_opts() | {"logger": QuietLogger()}
    if payload["mode"] == "info":
        with yt_dlp.YoutubeDL(opts | {"skip_download": True}) as ydl:
            info = ydl.extract_info(payload["url"], download=False)
        if not info or info.get("_type") in {"playlist", "multi_video"}:
            raise RuntimeError("Use a single media URL, rather than a playlist.")
        if info.get("is_live"):
            raise RuntimeError("Live streams are not supported by this bounded temporary downloader.")
        title = info.get("title") or "download"
        emit("result", dict(title=title, suggested_filename=_sanitize_stem(title),
            thumbnail=info.get("thumbnail"), duration=info.get("duration"), uploader=info.get("uploader"),
            webpage_url=info.get("webpage_url") or payload["url"], formats=clean_formats(info)))
        return
    directory = Path(payload["job_dir"])
    # User-entered names are applied only when serving the final file. yt-dlp output
    # templates interpret percent sequences, so use a fixed internal name.
    last_event = 0.0
    def progress(data):
        nonlocal last_event
        downloaded = data.get("downloaded_bytes") or 0
        if downloaded > MAX_FILESIZE:
            raise RuntimeError("Maximum download size exceeded.")
        if data.get("status") == "downloading":
            if time.monotonic() - last_event < 0.2:
                return
            last_event = time.monotonic()
            emit("progress", dict(status="downloading", progress=_progress_percent(data),
                downloaded_bytes=downloaded, total_bytes=data.get("total_bytes") or data.get("total_bytes_estimate"),
                speed=data.get("speed"), eta=data.get("eta")))
        elif data.get("status") == "finished":
            emit("progress", dict(status="processing", progress=100, speed=None, eta=None))
    def processing(data):
        if data.get("status") in {"started", "processing"}:
            emit("progress", dict(status="processing", progress=100, speed=None, eta=None))
    def match_filter(info, *, incomplete=False):
        if info.get("is_live"):
            return "Live streams are not supported."
        if (info.get("filesize") or 0) > MAX_FILESIZE:
            return "Maximum download size exceeded."
        return None
    opts |= dict(outtmpl=str(directory / "media.%(ext)s"), continuedl=False,
                 progress_hooks=[progress], postprocessor_hooks=[processing], match_filter=match_filter)
    if payload["audio_only"]:
        opts |= {"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}]}
    elif payload["format_id"]:
        opts["format"] = payload["format_id"]
    else:
        opts |= {"format": "bestvideo+bestaudio/best", "merge_output_format": "mp4"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([payload["url"]])
    candidates = [p for p in directory.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in MEDIA_EXTENSIONS]
    if not candidates:
        raise RuntimeError("No output was produced. The file may exceed the size limit or be unavailable.")
    output = max(candidates, key=lambda p: p.stat().st_size)
    final = directory / (_sanitize_stem(payload["requested_name"]) + output.suffix)
    output.rename(final)
    emit("result", {"file_path": str(final)})


if __name__ == "__main__":
    try:
        execute(json.loads(sys.stdin.readline()))
    except Exception as exc:
        if isinstance(exc, yt_dlp.utils.DownloadError):
            message = "Download failed. The source may be unavailable, protected, or exceed the size limit."
        elif isinstance(exc, OSError) and exc.errno == 28:
            message = "Insufficient server storage."
        elif isinstance(exc, OSError) and exc.errno == 27:
            message = "Maximum download size exceeded."
        elif isinstance(exc, RuntimeError):
            message = str(exc)
        else:
            message = "Media processing failed. Try another format or source."
        emit("error", message)
        sys.exit(1)
