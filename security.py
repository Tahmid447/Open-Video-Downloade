"""Owner-only login. Secrets stay in the config volume, never the application tree."""
import asyncio
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
ATTEMPTS = []


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384,r=8,p=1).hex()
    return salt + ':' + digest


def owner_hash(config):
    path = Path(config) / 'owner.json'
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError('Owner config must be a private regular file (0600).')
    return json.loads(path.read_text())['password_hash']


def session_key(token, config):
    return hashlib.sha256((token + ':' + (owner_hash(config) or '')).encode()).hexdigest()


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


async def login(password, store, config):
    now = time.time()
    ATTEMPTS[:] = [t for t in ATTEMPTS if now-t < 60]
    if len(ATTEMPTS) >= 10:
        raise HTTPException(429, 'Too many login attempts. Wait one minute.')
    ATTEMPTS.append(now)
    expected = owner_hash(config)
    if not expected:
        raise HTTPException(503, 'Owner login is not configured. Run manage.py set-password on the server.')
    actual = await asyncio.to_thread(password_hash, password, expected.split(':')[0])
    if not hmac.compare_digest(actual, expected):
        raise HTTPException(401, 'Incorrect password.')
    token = secrets.token_urlsafe(32)
    with store.db:
        store.db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
        store.db.execute('INSERT INTO sessions VALUES(?,?)', (session_key(token, config), now+SESSION_SECONDS))
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
