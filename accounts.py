"""Explicit YouTube file import or local Chrome connection; never returns session secrets."""
import os
import sys
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlparse
from fastapi import HTTPException


def youtube_url(url):
    host = (urlparse(url).hostname or '').lower()
    return host in {'youtube.com','www.youtube.com','m.youtube.com','youtu.be'}


def import_cookies(text, config, store):
    if len(text.encode()) > 200000 or not text.startswith(('# Netscape HTTP Cookie File','# HTTP Cookie File')):
        raise HTTPException(400, 'Use a Netscape cookie file containing only youtube.com cookies.')
    count = 0; expiry = []; session_cookie = False; normalized = ['# Netscape HTTP Cookie File']
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
        if name in AUTH_COOKIE_NAMES:
            if timestamp: expiry.append(timestamp)
            else: session_cookie = True
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
                  expires_at=max(expiry) if expiry and not session_cookie else None, verified_channels=[],
                  connection_id=uuid.uuid4().hex,session_state='unknown')
    store.put('account','youtube',record)
    return record


AUTH_COOKIE_NAMES = {'SID','SAPISID','__Secure-1PSID','__Secure-3PSID','__Secure-1PAPISID','__Secure-3PAPISID'}


def local_chrome_available():
    return sys.platform=='darwin' and os.getenv('OVD_LOCAL_BROWSER')=='1'


def account_status(store, config):
    stored=store.get('account','youtube')
    record=dict(stored or dict(provider='youtube',status='disconnected',verified_channels=[]))
    if stored and not record.get('connection_id'):
        record['connection_id']=uuid.uuid4().hex
        store.put('account','youtube',record)
    state=record.get('session_state') or ('usable' if record.get('status')=='verified' and record.get('verified_channels') else 'unknown')
    configured=bool(stored)
    if record.get('source')=='browser':
        if not local_chrome_available():state='unavailable'
    elif not (Path(config)/'youtube.cookies.txt').is_file():
        state='missing'
    elif record.get('expires_at') and record['expires_at']<=time.time():
        state='expired'
    record.update(configured=configured,session_state=state,
                  reconnect_required=state in {'expired','rejected','missing'},
                  can_attempt=configured and state in {'unknown','usable'},
                  membership_state='verified' if record.get('verified_channels') or record.get('membership_checks') else 'pending')
    if state in {'missing','unavailable'}:record['status']='disconnected'
    elif state=='expired':record['status']='expired'
    elif state in {'rejected','blocked'}:record['status']='needs_attention'
    return record


def resolve_account(url, requested, store, config):
    mode=requested
    if mode is None:mode=(store.get('settings','owner') or {}).get('account_mode','auto')
    if mode in {'','public'}:return ''
    if mode=='youtube' and (not youtube_url(url) or urlparse(url).scheme!='https'):
        raise HTTPException(400,'The YouTube connection can only be used for HTTPS YouTube URLs.')
    if not youtube_url(url) or urlparse(url).scheme!='https':return ''
    status=account_status(store,config)
    if not status['configured']:
        if mode=='youtube':raise HTTPException(409,'Connect YouTube in Settings first.')
        return ''
    if status['session_state'] in {'expired','rejected','blocked','unavailable','missing'}:
        messages={'expired':'Your saved YouTube session has expired. Sign into the saved account and recheck it in Settings.',
                  'rejected':'YouTube rejected the saved session. Sign into the saved account and recheck it in Settings.',
                  'blocked':'macOS blocked access to the saved Chrome session. Allow Keychain access, then recheck it in Settings.',
                  'unavailable':'This Chrome connection belongs to a local Mac. Connect an authorized server session in Settings.',
                  'missing':'The saved YouTube session is missing. Reconnect in Settings.'}
        raise HTTPException(409,messages[status['session_state']])
    return 'youtube'


def cookie_path(url, account, store, config):
    if not account:return None
    if account!='youtube' or not youtube_url(url) or urlparse(url).scheme!='https':
        raise HTTPException(400,'This connection can only be used for HTTPS YouTube URLs.')
    resolve_account(url,'youtube',store,config)
    status=account_status(store,config)
    if status.get('source')=='browser':return None
    path=Path(config)/'youtube.cookies.txt'
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise HTTPException(503,'Account secret permissions are invalid.')
    return str(path)


def record_success(info, store, downloaded=False):
    record=store.get('account','youtube')
    if not record:return
    record['last_checked_at']=time.time()
    # Public extraction success does not prove the provider accepted a login.
    if info.get('availability') in {'subscriber_only','private'} and (downloaded or info.get('formats')):
        record.update(session_state='usable',session_verified_at=time.time(),last_error=None,status='verified' if record.get('verified_channels') else 'connected')
        if info.get('availability')=='subscriber_only' and info.get('channel_id'):
            checks=record.get('membership_checks',{})
            previous=checks.get(info['channel_id'],{})
            checks[info['channel_id']]=dict(channel=info.get('uploader') or info['channel_id'],checked_at=time.time(),
                 evidence='download' if downloaded or previous.get('evidence')=='download' else 'analysis')
            record['membership_checks']=checks
            if downloaded:
                channels=set(record.get('verified_channels',[]));channels.add(info['channel_id'])
                record.update(status='verified',verified_channels=sorted(channels),last_verified_at=time.time())
    store.put('account','youtube',record)


def verified_download(info, store):
    record_success(info,store,downloaded=True)


def record_failure(message, store):
    record=store.get('account','youtube')
    if not record:return
    # Resource, format, network, bot-verification and channel eligibility failures
    # do not prove that a reusable provider session has expired or been revoked.
    state=None;safe=None
    if 'session has expired' in message:
        state='expired';safe='The saved YouTube session expired.'
    elif 'No Chrome cookie database was found' in message:
        state='missing';safe='The saved Chrome session is missing. Reconnect the selected profile.'
    elif 'No signed-in YouTube session was readable' in message:
        state='rejected';safe='The saved Chrome profile has no readable signed-in YouTube session.'
    elif 'macOS denied Chrome session access' in message:
        state='blocked';safe='macOS blocked Chrome session access; check the Keychain prompt.'
    elif 'did not accept this session as a member' in message:
        safe='Membership access was not confirmed for this video. Check the account and channel eligibility.'
    if safe:
        record.update(last_error=safe,last_failed_at=time.time())
        if state:record.update(session_state=state,status='needs_attention')
        store.put('account','youtube',record)


def worker_credentials(url, account, store, config):
    path=cookie_path(url,account,store,config)
    record=account_status(store,config) if account else {}
    if record.get('source')=='browser':
        return {'cookiebrowser':'chrome','cookieprofile':record['profile']}
    return {'cookiefile':path}


def chrome_connection(profile):
    if sys.platform!='darwin' or os.getenv('OVD_LOCAL_BROWSER')!='1':
        raise HTTPException(403,'Browser sessions are supported only in the explicitly enabled local Mac preview.')
    if not re.fullmatch(r'Default|Profile [0-9]+',profile):
        raise HTTPException(400,'Choose a Chrome profile from the local profile list.')
    folder=Path.home()/'Library/Application Support/Google/Chrome'/profile
    if not folder.is_dir():raise HTTPException(409,'Open Chrome and sign into YouTube first.')
    return dict(provider='youtube',source='browser',browser='chrome',profile=profile,status='unverified',session_state='unknown',
                connection_id=uuid.uuid4().hex,verified_channels=[],connected_at=time.time())


def connect_chrome(profile, store):
    record=chrome_connection(profile);store.put('account','youtube',record);return record


def chrome_profiles():
    if sys.platform!='darwin' or os.getenv('OVD_LOCAL_BROWSER')!='1':return []
    directory=Path.home()/'Library/Application Support/Google/Chrome'
    if not directory.is_dir():return []
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and re.fullmatch(r'Default|Profile [0-9]+',p.name))
