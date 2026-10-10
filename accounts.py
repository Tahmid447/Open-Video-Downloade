"""Explicit YouTube cookie import only; never reads a browser profile or password."""
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse
from fastapi import HTTPException


def youtube_url(url):
    host = (urlparse(url).hostname or '').lower()
    return host in {'youtube.com','www.youtube.com','m.youtube.com','youtu.be'}


def import_cookies(text, config, store):
    if len(text.encode()) > 200000 or not text.startswith(('# Netscape HTTP Cookie File','# HTTP Cookie File')):
        raise HTTPException(400, 'Use a Netscape cookie file containing only youtube.com cookies.')
    count = 0; expiry = []; normalized = ['# Netscape HTTP Cookie File']
    for line in text.splitlines()[1:]:
        if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        fields = line.removeprefix('#HttpOnly_').split('\t')
        if len(fields) != 7:
            raise HTTPException(400, 'Invalid cookie row.')
        domain, flag, path, secure, expires, name, value = fields
        if domain.lstrip('.').lower() not in {'youtube.com','www.youtube.com','m.youtube.com'}:
            raise HTTPException(400, 'Cookie file contains unrelated domains. Export only youtube.com.')
        if not expires.isdigit() or flag not in {'TRUE','FALSE'} or secure not in {'TRUE','FALSE'} or not path.startswith('/'):
            raise HTTPException(400, 'Invalid cookie fields.')
        if not name or not value or any(ord(c)<32 for c in name+value):
            raise HTTPException(400, 'Invalid cookie value.')
        timestamp = int(expires)
        if timestamp and timestamp <= time.time():
            continue
        if timestamp: expiry.append(timestamp)
        normalized.append(line); count += 1
    if not count:
        raise HTTPException(400, 'No unexpired cookies were supplied.')
    target = Path(config)/'youtube.cookies.txt'
    temporary = target.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,'w') as f:
        f.write('\n'.join(normalized)+'\n')
        f.flush(); os.fsync(f.fileno())
    os.replace(temporary,target)
    record = dict(provider='youtube', status='unverified', imported_at=time.time(),
                  expires_at=min(expiry) if expiry else None, verified_channels=[])
    store.put('account','youtube',record)
    return record


def account_status(store, config):
    record = store.get('account','youtube') or dict(provider='youtube',status='disconnected',verified_channels=[])
    if not (Path(config)/'youtube.cookies.txt').exists():
        record.update(status='disconnected',verified_channels=[])
    elif record.get('expires_at') and record['expires_at'] <= time.time():
        record['status'] = 'expired'
    return record


def cookie_path(url, account, store, config):
    if not account:
        return None
    if account != 'youtube' or not youtube_url(url) or urlparse(url).scheme != 'https':
        raise HTTPException(400, 'This connection can only be used for HTTPS YouTube URLs.')
    status = account_status(store, config)
    if status['status'] in {'disconnected','expired','needs_attention'}:
        raise HTTPException(409, 'Reconnect this account before using it.')
    path = Path(config)/'youtube.cookies.txt'
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise HTTPException(503, 'Account secret permissions are invalid.')
    return str(path)


def verified_download(info, store):
    record = store.get('account','youtube')
    if record and info.get('availability') == 'subscriber_only' and info.get('channel_id'):
        channels = set(record.get('verified_channels',[])); channels.add(info['channel_id'])
        record.update(status='verified', verified_channels=sorted(channels), last_verified_at=time.time())
        store.put('account','youtube',record)
