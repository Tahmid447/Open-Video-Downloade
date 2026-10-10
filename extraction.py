"""yt-dlp adapter: site extractors, then its built-in generic/embed/manifest support.
No arbitrary plugin loading, browser automation, DRM or authorization bypass.
"""
import hashlib
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from common import clean_formats, _sanitize_stem

HEIGHTS = (2160, 1440, 1080, 720, 480, 360)


def stable_key(url, info=None):
    info = info or {}
    provider = (info.get('extractor_key') or info.get('extractor') or '').lower()
    identifier = info.get('id')
    if provider and provider != 'generic' and identifier:
        return provider + ':' + str(identifier)
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if host == 'youtu.be' or host.endswith('.youtube.com') or host == 'youtube.com':
        video_id = (parsed.path.lstrip('/') if host == 'youtu.be' else parse_qs(parsed.query).get('v', [''])[0])
        if parsed.path.startswith(('/shorts/', '/embed/', '/live/')):
            video_id = parsed.path.split('/')[2]
        if video_id:
            return 'youtube:' + video_id
    # Remove tracking/fragment only. Signed and substantive query parameters remain.
    query = [(k,v) for k,values in parse_qs(parsed.query, keep_blank_values=True).items()
             if not k.startswith('utm_') and k not in {'fbclid','gclid'} for v in values]
    canonical = urlunparse((parsed.scheme.lower(),parsed.netloc.lower(),parsed.path,'',urlencode(sorted(query)),''))
    return 'url:' + hashlib.sha256(canonical.encode()).hexdigest()


def preview(info, url):
    if not info or info.get('_type') in {'playlist', 'multi_video'}:
        raise RuntimeError('Use a single media URL. Channel discovery belongs in Membership Import.')
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming'}:
        raise RuntimeError('Live streams and upcoming premieres are not supported. Use an archived recording.')
    if info.get('has_drm'):
        raise RuntimeError('DRM-protected media is unsupported.')
    formats = clean_formats(info)
    title = str(info.get('title') or 'download')[:1000]
    heights = {f['height'] for f in formats if f.get('height') and f['type'] != 'audio only'}
    audio = any(f['type'] != 'video only' for f in formats)
    return dict(title=title, suggested_filename=_sanitize_stem(title),
        media_key=stable_key(url, info), platform=info.get('extractor_key') or info.get('extractor') or urlparse(url).hostname,
        media_id=info.get('id'), thumbnail=info.get('thumbnail'), duration=info.get('duration'),
        uploader=info.get('uploader') or info.get('channel'), channel_id=info.get('channel_id'),
        upload_date=info.get('upload_date'), description=str(info.get('description') or '')[:8000],
        availability=info.get('availability'), webpage_url=info.get('webpage_url') or url,
        subtitles=sorted((info.get('subtitles') or {}).keys()),
        automatic_captions=sorted((info.get('automatic_captions') or {}).keys()),
        formats=formats, qualities=[h for h in HEIGHTS if h in heights], audio_available=audio)


def format_selector(payload):
    if payload.get('audio_only'):
        return 'bestaudio/best'
    if payload.get('format_id'):
        # Video-only rows are explicitly combined with audio in the UI.
        return payload['format_id']
    height = payload.get('quality')
    cap = f'[height<=?{height}]' if height else ''
    return f'bestvideo{cap}+bestaudio/best{cap}'


def member_entries(info, source, limit):
    accepted = {}
    seen = rejected = 0
    for entry in (info or {}).get('entries') or []:
        if seen >= limit:
            break
        seen += 1
        if not entry or entry.get('availability') != 'subscriber_only' or not entry.get('id') or entry.get('is_live'):
            rejected += 1
            continue
        identifier = str(entry['id'])
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
            rejected += 1
            continue
        key = 'youtube:' + identifier
        accepted[key] = dict(media_key=key, media_id=identifier, title=entry.get('title') or identifier,
            url='https://www.youtube.com/watch?v=' + identifier, thumbnail=entry.get('thumbnail') or
            next(iter(entry.get('thumbnails') or []), {}).get('url'), duration=entry.get('duration'),
            upload_date=entry.get('upload_date'), channel=entry.get('channel') or entry.get('uploader'),
            channel_id=entry.get('channel_id'), category='members-only', availability='subscriber_only',
            discovery_source=source, status='discovered')
    return dict(entries=list(accepted.values()), inspected=seen, excluded_unconfirmed=rejected,
                complete=False, limit=limit, note='Bounded scan. Only explicit members-only badges are included; unknown/public items are excluded. Coverage is not guaranteed.')
