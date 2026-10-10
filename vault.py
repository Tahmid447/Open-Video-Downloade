"""Authenticated encryption. Keys are separate private files, never data/config/env values."""
import base64
import os
from pathlib import Path
import stat
from Cryptodome.Cipher import AES

class VaultError(RuntimeError):
    pass


def validate_location(path,forbidden=()):
    path=Path(path)
    if not path.is_absolute():raise VaultError('Configure an absolute private encryption-key file outside data and source directories.')
    roots=[Path(__file__).resolve().parent,*map(Path,forbidden)]
    roots += [Path(os.environ[k]) for k in ['CONFIG_DIR','STATE_DIR','LIBRARY_DIR','DOWNLOAD_DIR'] if os.getenv(k)]
    resolved=path.resolve()
    if any(resolved==r.resolve() or r.resolve() in resolved.parents for r in roots):
        raise VaultError('Encryption keys must be separate from application data and source.')
    return path


def read_key(path=None, forbidden=()):
    path=validate_location(path or os.getenv('OVD_CREDENTIAL_KEY_FILE',''),forbidden)
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            metadata=os.fstat(f.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:raise ValueError()
            key=f.read(33)
        if len(key)!=32:raise ValueError()
    except (OSError,ValueError):raise VaultError('Encryption key unavailable or invalid; require a regular 0600 file containing 32 bytes.') from None
    return key


def initialize_key(path):
    path=validate_location(path)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.parent.stat().st_mode & 0o077:raise VaultError('Key directory must have private 0700 permissions.')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(os.urandom(32));f.flush();os.fsync(f.fileno())
    # Validate location and permissions; no value is printed or returned.
    read_key(path)


def seal(data,purpose,key=None):
    key=key or read_key();nonce=os.urandom(12)
    cipher=AES.new(key,AES.MODE_GCM,nonce=nonce);cipher.update(('ovd:v1:'+purpose).encode())
    encrypted,tag=cipher.encrypt_and_digest(data)
    return b'OVDSEC1\n'+base64.b64encode(nonce+tag+encrypted)


def unseal(data,purpose,key=None):
    key=key or read_key()
    try:
        if not data.startswith(b'OVDSEC1\n'):raise ValueError()
        raw=base64.b64decode(data[8:],validate=True)
        if len(raw)<28:raise ValueError()
        cipher=AES.new(key,AES.MODE_GCM,nonce=raw[:12]);cipher.update(('ovd:v1:'+purpose).encode())
        return cipher.decrypt_and_verify(raw[28:],raw[12:28])
    except (ValueError,KeyError):raise VaultError('Encrypted credentials failed authentication. Restore the matching key and verified backup; no fallback to plaintext.') from None


def atomic_write(path,data):
    path=Path(path);temporary=path.with_name(path.name+'.'+os.urandom(8).hex()+'.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temporary.unlink(missing_ok=True)


def encryption_required():return os.getenv('OVD_REQUIRE_ENCRYPTION')=='1'
