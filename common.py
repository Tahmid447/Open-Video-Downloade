import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse
from fastapi import HTTPException

MAX_FILESIZE = int(os.getenv("MAX_FILESIZE_BYTES", str(1024**3)))
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".wav", ".flac"}
def _is_public_ip(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_remote_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "Only http/https URLs are allowed.")
    if not parsed.hostname:
        raise HTTPException(400, "Invalid URL.")

    if parsed.username or parsed.password:
        raise HTTPException(400, "URLs containing credentials are not supported.")
    try:
        port = parsed.port
    except ValueError:
        raise HTTPException(400, "Invalid port.")
    if port not in {None, 80, 443}:
        raise HTTPException(400, "Only standard web ports are supported.")
    host = parsed.hostname.strip().lower()
    if host in {"localhost"} or host.endswith(".local"):
        raise HTTPException(400, "Local network URLs are not allowed.")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(400, "Host could not be resolved.")

    ips = {entry[4][0] for entry in infos}
    if not ips or any(not _is_public_ip(ip) for ip in ips):
        raise HTTPException(400, "Private, local, or reserved destinations are not allowed.")

    return url


def clean_formats(info: dict) -> list[dict]:
    seen = set()
    out = []

    for f in info.get("formats") or []:
        fmt_id = f.get("format_id")
        if not fmt_id or f.get("has_drm"):
            continue

        vcodec = f.get("vcodec")
        acodec = f.get("acodec")

        if vcodec == "none" and acodec == "none":
            continue

        width = f.get("width")
        height = f.get("height")
        fps = f.get("fps")
        tbr = f.get("tbr")
        vbr = f.get("vbr")
        abr = f.get("abr")
        ext = f.get("ext")
        protocol = f.get("protocol")
        filesize = f.get("filesize") or f.get("filesize_approx")
        note = f.get("format_note") or ""

        media_type = "video + audio"
        if vcodec == "none":
            media_type = "audio only"
        elif acodec == "none":
            media_type = "video only"

        signature = (
            fmt_id, width, height, fps, tbr, ext,
            vcodec, acodec, protocol,
        )
        if signature in seen:
            continue
        seen.add(signature)

        if width and height:
            resolution = f"{width}×{height}"
        elif height:
            resolution = f"{height}p"
        else:
            resolution = None

        out.append({
            "format_id": str(fmt_id),
            "type": media_type,
            "ext": ext,
            "width": width,
            "height": height,
            "resolution": resolution,
            "fps": fps,
            "tbr": tbr,
            "vbr": vbr,
            "abr": abr,
            "vcodec": vcodec,
            "acodec": acodec,
            "protocol": protocol,
            "format_note": note,
            "filesize": filesize,
            "filesize_kind": "exact" if f.get("filesize") else "estimated" if f.get("filesize_approx") else "unknown",
        })

    out.sort(
        key=lambda x: (
            0 if x["type"] == "video + audio"
            else 1 if x["type"] == "video only"
            else 2,
            -(x["height"] or 0),
            -(x["tbr"] or 0),
        )
    )

    return out[:120]


def _sanitize_stem(value: str | None, fallback: str = "download") -> str:
    """Return a safe filename stem while keeping normal Unicode names readable."""
    text = (value or "").strip()
    if not text:
        text = fallback

    # If the user typed a common media extension, avoid file.mp4.mp4.
    suffix = Path(text).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        text = text[:-len(suffix)]

    # Keep human-friendly names but remove path/control/reserved filename chars.
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:120].strip(" .")
    # Linux filename limits are bytes, while the UI limit is characters.
    text = text.encode("utf-8")[:180].decode("utf-8", errors="ignore").strip(" .")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", text):
        text = "_" + text
    return text or fallback


def ydl_base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": False,
        "max_filesize": MAX_FILESIZE,
        "retries": 2,
        "socket_timeout": 20,
        "proxy": "",
        "cachedir": False,
        "js_runtimes": {"deno": {}},
        "remote_components": set(),
        "allowed_extractors": ["default"],
        "allow_unplayable_formats": False,
        "hls_prefer_native": True,
        "concurrent_fragment_downloads": 1,
        "format_sort": ["res", "vcodec:h264", "acodec:aac"],
        # Session import is explicit and provider-scoped in accounts.py.
    }


def _progress_percent(data: dict) -> float | None:
    downloaded = data.get("downloaded_bytes") or 0
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    if total:
        return max(0.0, min(100.0, (downloaded / total) * 100))

    fragment_index = data.get("fragment_index")
    fragment_count = data.get("fragment_count")
    if fragment_index and fragment_count:
        return max(0.0, min(100.0, (fragment_index / fragment_count) * 100))

    return None


