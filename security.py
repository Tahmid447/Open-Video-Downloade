"""Owner-only login. Secrets stay in the config volume, never the application tree."""
import asyncio
import base64
import struct
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import time
from fastapi import HTTPException

SESSION_SECONDS = 7 * 86400
ATTEMPTS = []  # Compatibility for existing test fixtures; production uses SQLite rate limits.


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384,r=8,p=1).hex()
    return salt + ':' + digest


def owner_config(config):
    path = Path(config) / 'owner.json'
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError('Owner config must be a private regular file (0600).')
    return json.loads(path.read_text())


def owner_hash(config):
    return (owner_config(config) or {}).get('password_hash')


def write_owner(config, data):
    directory = Path(config)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory/'owner.json'
    temporary = directory/'owner.json.new'
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def totp(secret, counter):
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return str((struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff) % 1000000).zfill(6)


def verify_totp(data, code, field='totp_secret'):
    if len(code)!=6 or not code.isdigit() or not data.get(field):
        return False
    counter = int(time.time()//30)
    for step in (counter, counter-1, counter+1):
        if step > data.get('totp_last', -1) and hmac.compare_digest(totp(data[field], step), code):
            data['totp_last'] = step
            return True
    return False


def loopback_setup(request):
    if os.getenv('OVD_LOCAL_SETUP')!='1' or not request.client:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback and request.headers.get('host') in {'127.0.0.1:8001','localhost:8001'}
    except ValueError:
        return False


def rate_limit(store):
    now=time.time()
    # Persist across server restarts; a single-owner global limit also caps distributed guesses.
    attempts=store.get('security','attempts') or {'times':[]}
    times=[t for t in attempts['times'] if now-t<60]
    if len(times)>=10:
        raise HTTPException(429,'Too many login attempts. Wait one minute.')
    store.put('security','attempts',{'times':times+[now]})


def mfa_ready(config):
    return bool((owner_config(config) or {}).get('totp_secret'))


def session_key(token, config):
    data=owner_config(config) or {}
    return hashlib.sha256((token + ':' + data.get('password_hash','') + ':' + data.get('totp_secret','')).encode()).hexdigest()

def authenticated(request, store, config):
    token = request.cookies.get('ovd_session','')
    if not token or len(token) > 200:
        return False
    key = session_key(token, config)
    row = store.db.execute('SELECT expires FROM sessions WHERE hash=?', (key,)).fetchone()
    return bool(row and row[0] > time.time())


def local_development(request):
    # Explicit local fixture mode cannot be enabled for remote/tailnet clients.
    if os.getenv('OVD_ALLOW_UNAUTHENTICATED_LOCAL') != '1' or not request.client:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


async def check_password(password, config):
    expected=owner_hash(config)
    if not expected:
        raise HTTPException(503,'Owner login is not configured.')
    actual=await asyncio.to_thread(password_hash,password,expected.split(':')[0])
    if not hmac.compare_digest(actual,expected):
        raise HTTPException(401,'Incorrect password or authentication code.')


async def login(password, store, config, code=''):
    rate_limit(store)
    await check_password(password,config)
    data=owner_config(config)
    if data.get('totp_secret'):
        if not verify_totp(data,code):
            raise HTTPException(401,'Incorrect password or authentication code.')
        write_owner(config,data)
    token=secrets.token_urlsafe(32)
    now=time.time()
    with store.db:
        store.db.execute('DELETE FROM sessions WHERE expires < ?',(now,))
        store.db.execute('INSERT INTO sessions VALUES(?,?)',(session_key(token,config),now+SESSION_SECONDS))
    return token


class BodyLimit:
    def __init__(self, app, limit=256*1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in {'POST','PUT','PATCH'}:
            return await self.app(scope, receive, send)
        chunks = []; size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body = message.get('body', b''); size += len(body)
            if size > self.limit:
                await send({'type':'http.response.start','status':413,'headers':[(b'content-type',b'application/json')]})
                return await send({'type':'http.response.body','body':b'{"detail":"Request too large."}'})
            chunks.append(body)
            if not message.get('more_body'):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type':'http.request','body':b''.join(chunks),'more_body':False}
            return await receive()
        await self.app(scope, replay, send)
