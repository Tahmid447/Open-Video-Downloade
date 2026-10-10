"""Offline, streaming encrypted backups. Restore into a new directory, never over live data."""
import fcntl
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tarfile
import tempfile
import time
from Cryptodome.Cipher import AES
from database import Store
import vault

CHUNK=1024*1024
HEADER=b'OVDBAK2\n'


def lock_state(state):
    state=Path(state);state.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(state/'maintenance.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    stream=os.fdopen(fd,'rb+')
    try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close();raise RuntimeError('Stop the application before backup, recovery or key rotation.') from None
    return stream


class EncryptedWriter:
    # Independently authenticated bounded chunks avoid GCM's per-message size limit.
    def __init__(self,stream,key):
        self.stream=stream;self.key=key;self.prefix=os.urandom(8);self.counter=0
        stream.write(HEADER+self.prefix)
    def record(self,data):
        index=self.counter.to_bytes(4,'big');length=len(data).to_bytes(4,'big')
        cipher=AES.new(self.key,AES.MODE_GCM,nonce=self.prefix+index);cipher.update(HEADER+index+length)
        encrypted,tag=cipher.encrypt_and_digest(data);self.stream.write(length+encrypted+tag);self.counter+=1
    def write(self,data):
        for start in range(0,len(data),CHUNK):self.record(data[start:start+CHUNK])
        return len(data)
    def finish(self):self.record(b'')  # Authenticated end marker makes truncation fail closed.


def validate_secrets(config,key):
    config=Path(config)
    for path in config.iterdir():
        if not path.is_file() or path.is_symlink():raise vault.VaultError('Secret config must contain regular files only.')
        if path.stat().st_mode & 0o077:raise vault.VaultError('Secret configuration files must be private (0600).')
        if path.name=='owner.json':vault.unseal(path.read_bytes(),'owner',key)
        elif path.name.startswith('youtube-') and path.suffix=='.enc':
            import re
            if not re.fullmatch(r'youtube-[a-f0-9]{32}\.enc',path.name):raise vault.VaultError('Unknown credential file.')
            vault.unseal(path.read_bytes(),'youtube:'+path.stem[8:],key)
        else:raise vault.VaultError('Unknown or plaintext credential config; explicit migration/reprovisioning required.')


def backup(config,state,library,output,backup_key_file):
    roots={k:Path(v).resolve() for k,v in [('config',config),('state',state),('library',library)]}
    key=vault.read_key(forbidden=roots.values());backup_key=vault.read_key(backup_key_file,forbidden=roots.values())
    if key==backup_key:raise vault.VaultError('Use a separate backup encryption key.')
    output=Path(output).absolute()
    if any(r==output or r in output.parents for r in roots.values()):raise RuntimeError('Backup output must be outside application data.')
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with lock_state(state),tempfile.TemporaryDirectory(prefix='ovd-backup-',dir=output.parent) as temp:
        validate_secrets(config,key)
        # Media rarely compresses. Reserve room before creating a snapshot/archive.
        source_bytes=sum(p.stat().st_size for root in roots.values() for p in root.rglob('*') if p.is_file())
        required=source_bytes+source_bytes//20+1024**3
        if shutil.disk_usage(output.parent).free<required:
            raise RuntimeError('Insufficient backup space; choose an independent private destination with room for the library and 1 GiB reserve.')
        snapshot=Path(temp)/'library.sqlite3';store=Store(roots['state']/'library.sqlite3')
        try:store.backup(snapshot)
        finally:store.close()
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:
                encrypted=EncryptedWriter(stream,backup_key)
                with tarfile.open(fileobj=encrypted,mode='w|gz') as archive:
                    manifest=json.dumps({'version':1,'roots':{k:str(v) for k,v in roots.items()}}).encode()
                    entry=tarfile.TarInfo('manifest.json');entry.size=len(manifest);entry.mode=0o600
                    archive.addfile(entry,io.BytesIO(manifest));archive.add(snapshot,arcname='state/library.sqlite3',recursive=False)
                    for kind in ['config','library']:
                        for path in sorted(roots[kind].rglob('*')):
                            if path.is_symlink() or not (path.is_file() or path.is_dir()):raise RuntimeError('Backup refuses symlinks and special files.')
                            archive.add(path,arcname=kind+'/'+str(path.relative_to(roots[kind])),recursive=False)
                encrypted.finish();stream.flush();os.fsync(stream.fileno())
        except BaseException:output.unlink(missing_ok=True);raise


def decrypt_archive(source,output,key,max_bytes):
    with open(source,'rb') as stream,open(output,'xb') as dest:
        if stream.read(8)!=HEADER:raise vault.VaultError('Unsupported backup version.')
        prefix=stream.read(8);counter=0;total=0
        if len(prefix)!=8:raise vault.VaultError('Truncated encrypted backup.')
        while True:
            length_raw=stream.read(4)
            if len(length_raw)!=4:raise vault.VaultError('Truncated encrypted backup.')
            length=int.from_bytes(length_raw,'big');total+=length
            if length>CHUNK or total>max_bytes:raise vault.VaultError('Backup exceeds the recovery size limit.')
            encrypted=stream.read(length);tag=stream.read(16);index=counter.to_bytes(4,'big');counter+=1
            cipher=AES.new(key,AES.MODE_GCM,nonce=prefix+index);cipher.update(HEADER+index+length_raw)
            try:data=cipher.decrypt_and_verify(encrypted,tag)
            except ValueError:raise vault.VaultError('Backup authentication failed. No restored data was published.') from None
            if not length:
                if stream.read(1):raise vault.VaultError('Unexpected trailing backup data.')
                break
            dest.write(data)
    os.chmod(output,0o600)


def restore(source,output,backup_key_file,max_bytes=200*1024**3):
    output=Path(output).absolute()
    if output.exists():raise RuntimeError('Restore requires a new, nonexistent output directory; live data is never overwritten.')
    key=vault.read_key(forbidden=[output]);backup_key=vault.read_key(backup_key_file,forbidden=[output])
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='ovd-restore-',dir=output.parent) as temp:
        temp=Path(temp);plain=temp/'archive.tar.gz';staging=temp/'restored';staging.mkdir(mode=0o700)
        decrypt_archive(source,plain,backup_key,max_bytes)
        total=0;count=0;seen=set()
        with tarfile.open(plain,mode='r|gz') as archive:
            for entry in archive:
                name=PurePosixPath(entry.name);count+=1
                if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0] not in {'manifest.json','config','state','library'} or not (entry.isfile() or entry.isdir()) or entry.name in seen:
                    raise RuntimeError('Unsafe backup member refused.')
                seen.add(entry.name);total+=entry.size
                if count>100000 or total>max_bytes:raise RuntimeError('Backup exceeds recovery limits.')
                dest=staging/entry.name
                if entry.isdir():dest.mkdir(mode=0o700,parents=True,exist_ok=True)
                else:
                    dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as f,archive.extractfile(entry) as data:shutil.copyfileobj(data,f,CHUNK)
        manifest=json.loads((staging/'manifest.json').read_text())
        if manifest['version']!=1:raise RuntimeError('Unsupported backup manifest.')
        validate_secrets(staging/'config',key)
        db=sqlite3.connect(staging/'state/library.sqlite3')
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Restored metadata failed integrity check.')
            rows=db.execute('SELECT kind,id,data FROM records').fetchall()
            def relocate(value):
                if isinstance(value,dict):return {k:relocate(v) for k,v in value.items()}
                if isinstance(value,list):return [relocate(v) for v in value]
                if isinstance(value,str):
                    for kind,old in manifest['roots'].items():
                        if value==old or value.startswith(old+'/'):return str(output/kind)+value[len(old):]
                return value
            with db:
                db.execute('DELETE FROM sessions');db.execute("DELETE FROM records WHERE kind='reauth'")
                for kind,identity,data in rows:
                    if kind!='reauth':db.execute('UPDATE records SET data=? WHERE kind=? AND id=?',(json.dumps(relocate(json.loads(data))),kind,identity))
        finally:db.close()
        (staging/'manifest.json').unlink()
        # Validate each credential blob before publication. Browser sessions are not portable.
        store=Store(staging/'state/library.sqlite3')
        try:
            record=store.get('account','youtube') or {}
            if record.get('source')=='browser':raise RuntimeError('Mac Chrome connections cannot be restored onto a cloud server. Provision explicitly.')
            if record.get('source')=='encrypted':
                import accounts
                vault.unseal(accounts.credential_file(record,staging/'config').read_bytes(),'youtube:'+record['connection_id'],key)
        finally:store.close()
        owner=staging/'config/owner.json'
        if owner.exists():
            data=json.loads(vault.unseal(owner.read_bytes(),'owner',key))
            if data.get('totp_secret'):data['totp_last']=max(data.get('totp_last',-1),int(time.time()//30))
            vault.atomic_write(owner,vault.seal(json.dumps(data).encode(),'owner',key))
        os.rename(staging,output)


def rekey_config(config,state,output,new_key_file):
    config=Path(config);output=Path(output).absolute()
    if output.exists():raise RuntimeError('Key rotation requires a new output directory.')
    old=vault.read_key(forbidden=[config,state,output]);new=vault.read_key(new_key_file,forbidden=[config,state,output])
    if old==new:raise vault.VaultError('Choose a different encryption key.')
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with lock_state(state),tempfile.TemporaryDirectory(prefix='ovd-rekey-',dir=output.parent) as temp:
        validate_secrets(config,old);staging=Path(temp)/'config';staging.mkdir(mode=0o700)
        for path in config.iterdir():
            purpose='owner' if path.name=='owner.json' else 'youtube:'+path.stem[8:]
            vault.atomic_write(staging/path.name,vault.seal(vault.unseal(path.read_bytes(),purpose,old),purpose,new))
        validate_secrets(staging,new);os.rename(staging,output)
