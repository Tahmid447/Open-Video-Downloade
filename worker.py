"""Short-lived yt-dlp worker; stdout is bounded JSON events, never console logs."""
import json
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlparse
from pathlib import Path
import resource
import sys
import time
import yt_dlp
from yt_dlp.globals import plugin_dirs
plugin_dirs.value = []
from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser
from extraction import preview, format_selector, member_entries
from common import MAX_FILESIZE, MEDIA_EXTENSIONS, _progress_percent, _sanitize_stem, clean_formats, ydl_base_opts


def emit(kind, data):
    print(json.dumps({"type": kind, "data": data}), flush=True)


class SecureSessionPolicy(DefaultCookiePolicy):
    def return_ok(self, cookie, request):
        return urlparse(request.get_full_url()).scheme == 'https' and super().return_ok(cookie, request)


class QuietLogger:
    keychain_failed=False
    def debug(self, _message, *args, **kwargs): pass
    def info(self, _message, *args, **kwargs): pass
    def warning(self, _message, *args, **kwargs):
        if 'find-generic-password failed' in _message:self.keychain_failed=True
    def error(self, _message, *args, **kwargs): pass


def browser_session(payload):
    import os,re
    if sys.platform!='darwin' or os.getenv('OVD_LOCAL_BROWSER')!='1' or payload.get('cookiebrowser')!='chrome':
        raise RuntimeError('Local Chrome session access is disabled on this server.')
    profile=payload.get('cookieprofile','Default')
    if not re.fullmatch(r'Default|Profile [0-9]+',profile):raise RuntimeError('Invalid Chrome profile selection.')
    logger=QuietLogger()
    try:
        original=extract_cookies_from_browser('chrome',profile=profile,logger=logger)
    except FileNotFoundError:
        raise RuntimeError('No Chrome cookie database was found for this profile. Open Chrome, sign into YouTube, and retry.') from None
    except PermissionError:
        raise RuntimeError('macOS denied Chrome session access. Allow the local Python request in the macOS Keychain prompt, then retry.') from None
    except Exception:
        raise RuntimeError('Chrome session could not be read or decrypted. Check the macOS Keychain prompt and the selected Chrome profile.') from None
    expired_auth=any(c.name in {'SID','SAPISID','__Secure-1PSID','__Secure-3PSID','__Secure-1PAPISID','__Secure-3PAPISID'} and c.domain.lstrip('.').lower() in {'youtube.com','www.youtube.com','m.youtube.com'} and c.is_expired() for c in original)
    scoped=YoutubeDLCookieJar()
    for cookie in original:
        if cookie.domain.lstrip('.').lower() in {'youtube.com','www.youtube.com','m.youtube.com'} and not cookie.is_expired():scoped.set_cookie(cookie)
    original.clear(); scoped.filename=None; scoped.set_policy(SecureSessionPolicy())
    if not any(c.name in {'SID','SAPISID','__Secure-1PSID','__Secure-3PSID','__Secure-1PAPISID','__Secure-3PAPISID'} for c in scoped):
        if logger.keychain_failed:raise RuntimeError('macOS denied Chrome session access. Allow the local Python request in the macOS Keychain prompt, then retry.')
        if expired_auth:raise RuntimeError('The saved YouTube session has expired. Sign into YouTube in the saved Chrome profile and recheck it in Settings.')
        raise RuntimeError('No signed-in YouTube session was readable in this Chrome profile. Sign into YouTube in that profile and allow Keychain access if prompted.')
    return scoped


def source_failure(exc, authenticated=True):
    # Classify internally; never return raw extractor diagnostics, URLs, headers or tokens.
    text=str(exc).lower()
    if any(x in text for x in ['not a bot','confirm you’re not','confirm you\'re not']):return 'YouTube requires browser verification and refused the downloader session. No bypass was attempted.'
    if any(x in text for x in ['members-only','members only','join this channel']):
        return ('YouTube did not accept this session as a member for this video. Verify the selected account can play it.' if authenticated else 'This members-only video was requested without a YouTube connection. Connect YouTube in Settings, or switch Public mode to Automatic and analyze again.')
    if 'private video' in text:return 'YouTube reports this video is private or unavailable to this account.'
    if 'age' in text and ('confirm' in text or 'restricted' in text):return 'YouTube requires account age verification in the browser.'
    if any(x in text for x in ['po token','proof of origin']):return 'YouTube requires a proof-of-origin token for this request. This app has no token provider; no bypass was attempted.'
    if '403' in text:return 'YouTube or its media server refused access with HTTP 403.'
    if '429' in text:return 'YouTube rate-limited this request with HTTP 429. Stop and retry later.'
    if 'requested format is not available' in text:return 'YouTube did not provide a usable requested format for this session.'
    if 'video unavailable' in text or 'not available' in text:return 'YouTube reports this video is unavailable to this downloader session.'
    return 'Download failed. The source may be unavailable, protected, or exceed the size limit.'


def execute(payload):
    from network_guard import install
    install()
    # This limit is inherited by FFmpeg and also bounds unknown-size sources.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILESIZE, MAX_FILESIZE))
    opts = ydl_base_opts() | {"logger": QuietLogger()}
    memory_session=browser_session(payload) if payload.get("cookiebrowser") else None
    def new_ydl(options):
        ydl = yt_dlp.YoutubeDL(options)
        if memory_session is not None:
            ydl.cookiejar=memory_session
        elif payload.get('cookiefile'):
            # Load read-only: disconnect/replacement is coordinated by the parent.
            jar=YoutubeDLCookieJar(payload['cookiefile']); jar.load(ignore_discard=True,ignore_expires=False)
            jar.filename=None
            jar.set_policy(SecureSessionPolicy())
            ydl.cookiejar=jar
        return ydl
    if payload['mode']=='scan':
        options=opts | dict(skip_download=True,noplaylist=False,extract_flat=True,lazy_playlist=True,
                           playlistend=payload['limit'],sleep_interval_requests=1)
        with new_ydl(options) as ydl:
            info=ydl.extract_info(payload['url'],download=False)
            channel_id=(info or {}).get('channel_id') or (info or {}).get('uploader_id')
            if channel_id not in payload['verified_channels']:
                raise RuntimeError('First verify a members-only download from this specific channel.')
            result=member_entries(info,payload['url'],payload['limit'])
        emit('result',result)
        return
    if payload["mode"] == "info":
        with new_ydl(opts | {"skip_download": True,"extract_flat":"in_playlist","playlistend":10}) as ydl:
            info = ydl.extract_info(payload["url"], download=False)
        if info and info.get('_type') in {'multi_video','playlist'} and str(info.get('extractor_key','')).lower()=='generic':
            choices=[]; seen=set()
            for entry in list(info.get('entries') or [])[:10]:
                if not entry: continue
                url=entry.get('url') or entry.get('webpage_url')
                if url and url!=payload['url'] and url not in seen and url.startswith(('https://','http://')):
                    seen.add(url)
                    choices.append(dict(title=entry.get('title') or 'Embedded media',url=url))
            if choices:
                emit('result',dict(title=info.get('title') or 'Embedded media',kind='embedded_choices',choices=choices,formats=[]))
                return
        emit('result',preview(info,payload['url']))
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
    metadata = {}
    def match_filter(info, *, incomplete=False):
        nonlocal metadata
        if info.get('_type') in {'playlist','multi_video'}:
            return 'Use a single media URL.'
        if info.get('has_drm'):
            return 'DRM-protected media is unsupported.'
        if not incomplete:
            metadata = preview(info,payload['url'])
            # Formats belong in the preview, not persistent history.
            metadata.pop('formats',None)
        if info.get("is_live"):
            return "Live streams are not supported."
        if (info.get("filesize") or 0) > MAX_FILESIZE:
            return "Maximum download size exceeded."
        return None
    opts |= dict(outtmpl=str(directory / "media.%(ext)s"), continuedl=False,
                 progress_hooks=[progress], postprocessor_hooks=[processing], match_filter=match_filter)
    if payload.get("audio_only"):
        opts |= {"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}]}
    elif payload["format_id"]:
        opts["format"] = format_selector(payload)
        opts["merge_output_format"] = "mp4"
    else:
        opts |= {"format": format_selector(payload), "merge_output_format": "mp4"}
    with new_ydl(opts | {'extract_flat':'in_playlist','playlistend':10}) as ydl:
        # Validate the single item before starting any transfer; playlists never download accidentally.
        emit('progress',dict(status='analyzing',progress=None))
        info=ydl.extract_info(payload['url'],download=False)
        if payload.get('members_only') and (info or {}).get('availability')!='subscriber_only':
            raise RuntimeError('Membership import requires confirmed members-only status. This item was not downloaded.')
        metadata=preview(info,payload['url']); metadata.pop('formats',None)
        emit('progress',dict(status='ready',progress=0))
        ydl.process_ie_result(info,download=True)
    candidates = [p for p in directory.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in MEDIA_EXTENSIONS]
    if not candidates:
        raise RuntimeError("No output was produced. The file may exceed the size limit or be unavailable.")
    output = max(candidates, key=lambda p: p.stat().st_size)
    final = directory / (_sanitize_stem(metadata.get("suggested_filename") if payload["requested_name"]=="download" else payload["requested_name"]) + output.suffix)
    output.rename(final)
    emit("result", {"file_path": str(final), "metadata": metadata})


if __name__ == "__main__":
    try:
        payload=json.loads(sys.stdin.readline())
        execute(payload)
    except Exception as exc:
        if isinstance(exc, yt_dlp.utils.DownloadError):
            message = source_failure(exc,authenticated=bool(locals().get('payload',{}).get('cookiebrowser') or locals().get('payload',{}).get('cookiefile')))
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
