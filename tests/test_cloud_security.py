"""Synthetic-only credentials: encryption, authorization, verified replacement and recovery."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import time
import pytest
from fastapi.testclient import TestClient
from test_app import client
from test_account_workflow import member_info, URL
from test_mfa import PASSWORD, SECRET
import app as service
import accounts
import security
import recovery
import vault
from database import Store

COOKIES='# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsynthetic-session\n'
BODY={'cookies':COOKIES,'authorized':True,'test_url':URL}


def login_owner(client,monkeypatch,mfa=False):
    monkeypatch.setattr(security,'local_development',lambda request:False)
    monkeypatch.setenv('COOKIE_SECURE','0');monkeypatch.setenv('OVD_REQUIRE_MFA','1' if mfa else '0')
    data={'password_hash':security.password_hash(PASSWORD)}
    if mfa:data['totp_secret']=SECRET
    security.write_owner(service.CONFIG_DIR,data)
    assert client.post('/api/auth/login',json={'password':PASSWORD,'code':security.totp(SECRET,int(time.time()//30)) if mfa else ''}).status_code==200


def grant(client,action='youtube-replace',code=''):
    result=client.post('/api/auth/reauth',json={'password':PASSWORD,'code':code,'action':action})
    assert result.status_code==200
    return {'X-OVD-Reauth':result.json()['grant']}


def test_owner_mfa_and_provider_credentials_are_encrypted(client):
    security.write_owner(service.CONFIG_DIR,{'password_hash':security.password_hash(PASSWORD),'totp_secret':SECRET})
    accounts.import_cookies(COOKIES,service.CONFIG_DIR,service.STORE)
    for path in service.CONFIG_DIR.iterdir():
        raw=path.read_bytes();assert raw.startswith(b'OVDSEC1\n')
        assert SECRET.encode() not in raw and b'synthetic-session' not in raw and b'password_hash' not in raw
        assert path.stat().st_mode&0o777==0o600
    assert security.owner_config(service.CONFIG_DIR)['totp_secret']==SECRET
    assert accounts.worker_credentials(URL,'youtube',service.STORE,service.CONFIG_DIR)['cookie_text']==COOKIES
    assert 'synthetic-session' not in service.STORE.path.read_bytes().decode(errors='ignore')
    assert not (service.CONFIG_DIR/'youtube.cookies.txt').exists()
    assert 'cookie_text' not in client.get('/api/accounts').text


@pytest.mark.parametrize('failure',['missing','permissions','wrong','tamper','purpose'])
def test_vault_failures_never_fall_back_to_plaintext(client,monkeypatch,tmp_path,failure):
    key=Path(os.environ['OVD_CREDENTIAL_KEY_FILE']);sealed=vault.seal(COOKIES.encode(),'youtube:test')
    if failure=='missing':key.unlink()
    elif failure=='permissions':key.chmod(0o644)
    elif failure=='wrong':key.write_bytes(os.urandom(32))
    elif failure=='tamper':sealed=sealed[:-5]+b'AAAAA'
    with pytest.raises(vault.VaultError):vault.unseal(sealed,'owner' if failure=='purpose' else 'youtube:test')
    assert client.get('/api/health').status_code==(503 if failure in {'missing','permissions'} else 200)


def test_key_location_and_symlink_rejected(client,tmp_path):
    inside=service.CONFIG_DIR/'bad.key';inside.write_bytes(os.urandom(32));inside.chmod(0o600)
    with pytest.raises(vault.VaultError):vault.read_key(inside,forbidden=[service.CONFIG_DIR])
    symlink=tmp_path/'key-link';symlink.symlink_to(os.environ['OVD_CREDENTIAL_KEY_FILE'])
    with pytest.raises(vault.VaultError):vault.read_key(symlink)
    assert 'OVD_CREDENTIAL_KEY_FILE' not in client.get('/api/accounts').text


def test_plaintext_owner_config_refused_in_production(client):
    path=service.CONFIG_DIR/'owner.json';path.write_text(json.dumps({'password_hash':'synthetic'}));path.chmod(0o600)
    with pytest.raises(vault.VaultError):security.owner_config(service.CONFIG_DIR)
    assert client.get('/api/health').status_code==503


def test_reauthentication_wrong_password_action_session_replay_and_expiry(client,monkeypatch):
    login_owner(client,monkeypatch)
    assert client.post('/api/accounts/youtube',json=BODY).status_code==403
    assert client.post('/api/auth/reauth',json={'password':'incorrect-password','action':'youtube-replace'}).status_code==401
    authorization=grant(client)
    assert client.delete('/api/accounts/youtube',headers=authorization).status_code==403
    second=TestClient(service.app)
    assert second.post('/api/auth/login',json={'password':PASSWORD}).status_code==200
    assert second.post('/api/accounts/youtube',json=BODY,headers=authorization).status_code==403
    async def info(*args):return member_info()
    monkeypatch.setattr(service,'run_worker',info)
    assert client.post('/api/accounts/youtube',json=BODY,headers=authorization).status_code==200
    assert client.post('/api/accounts/youtube',json=BODY,headers=authorization).status_code==403
    authorization=grant(client)
    for identity,value in service.STORE.all('reauth').items():value['expires']=0;service.STORE.put('reauth',identity,value)
    assert client.post('/api/accounts/youtube',json=BODY,headers=authorization).status_code==403


def test_sensitive_actions_require_fresh_mfa_and_durable_throttling(client,monkeypatch):
    now=[2000000000];monkeypatch.setattr(security.time,'time',lambda:now[0]);login_owner(client,monkeypatch,True)
    action={'password':PASSWORD,'action':'youtube-replace'}
    assert client.post('/api/auth/reauth',json=action).status_code==401
    assert client.post('/api/auth/reauth',json=action|{'code':security.totp(SECRET,int(now[0]//30))}).status_code==401
    now[0]+=60;code=security.totp(SECRET,int(now[0]//30))
    authorization=grant(client,code=code)
    assert authorization and client.post('/api/auth/reauth',json=action|{'code':code}).status_code==401
    assert len(service.STORE.get('security','attempts')['times'])>=2
    assert b'totp_secret' not in (service.CONFIG_DIR/'owner.json').read_bytes()


def test_replacement_verified_atomic_and_old_connection_kept_on_refusal(client,monkeypatch):
    login_owner(client,monkeypatch);old=accounts.import_cookies(COOKIES,service.CONFIG_DIR,service.STORE);old_path=accounts.credential_file(old,service.CONFIG_DIR);old_bytes=old_path.read_bytes()
    calls=[]
    async def public(payload,*args):calls.append(payload);return member_info()|{'availability':'public'}
    monkeypatch.setattr(service,'run_worker',public)
    response=client.post('/api/accounts/youtube',json=BODY,headers=grant(client))
    assert response.status_code==422 and service.STORE.get('account','youtube')['connection_id']==old['connection_id'] and old_path.read_bytes()==old_bytes
    assert calls[0]['url']==URL and calls[0]['cookie_text']==COOKIES and not calls[0].get('cookiebrowser')
    async def valid(*args):return member_info()
    monkeypatch.setattr(service,'run_worker',valid)
    client.put('/api/settings',json={'account_mode':'public'})
    response=client.post('/api/accounts/youtube',json=BODY,headers=grant(client));new=response.json()['account']
    assert new['connection_id']!=old['connection_id'] and new['session_state']=='usable' and not old_path.exists()
    assert client.get('/api/settings').json()['account_mode']=='public'
    assert len(list(service.CONFIG_DIR.glob('youtube-*.enc')))==1 and 'synthetic-session' not in response.text


def test_owner_revocation_during_verification_prevents_commit(client,monkeypatch):
    login_owner(client,monkeypatch)
    async def revoke(*args):
        with service.STORE.db:service.STORE.db.execute('DELETE FROM sessions')
        return member_info()
    monkeypatch.setattr(service,'run_worker',revoke)
    response=client.post('/api/accounts/youtube',json=BODY,headers=grant(client))
    assert response.status_code==401 and not service.STORE.get('account','youtube')
    assert not list(service.CONFIG_DIR.glob('youtube-*.enc'))


def test_db_commit_failure_keeps_old_encrypted_generation(client,monkeypatch):
    old=accounts.import_cookies(COOKIES,service.CONFIG_DIR,service.STORE)
    put=service.STORE.put
    def fail(kind,*args):
        if kind=='account':raise RuntimeError('synthetic commit interruption')
        return put(kind,*args)
    monkeypatch.setattr(service.STORE,'put',fail)
    with pytest.raises(RuntimeError):accounts.import_cookies(COOKIES,service.CONFIG_DIR,service.STORE)
    assert service.STORE.get('account','youtube')['connection_id']==old['connection_id']
    assert len(list(service.CONFIG_DIR.glob('youtube-*.enc')))==1


def test_active_jobs_block_change_and_plaintext_not_in_persistent_jobs(client,monkeypatch):
    login_owner(client,monkeypatch);accounts.import_cookies(COOKIES,service.CONFIG_DIR,service.STORE,member_info())
    async def info(*args):return member_info()
    monkeypatch.setattr(service,'run_worker',info);client.post('/api/queue',json={'paused':True})
    preview=client.post('/api/info',json={'url':URL,'account':'auto'}).json()
    job=client.post('/api/download/start',json={'url':URL,'account':'youtube','analysis_id':preview['analysis_id'],'enqueue':True}).json()['job_id']
    assert client.post('/api/accounts/youtube',json=BODY,headers=grant(client)).status_code==409
    assert 'cookie_text' not in json.dumps(service.STORE.get('job',job))
    assert 'synthetic-session' not in json.dumps(service.STORE.all('preview'))
    client.post('/api/download/cancel/'+job)


def dataset(tmp_path,monkeypatch):
    root=tmp_path/'data';config=root/'config';state=root/'state';library=root/'library'
    for path in [config,state,library]:path.mkdir(mode=0o700,parents=True)
    key=tmp_path/'keys/master.key';backup_key=tmp_path/'backup-keys/master.key';vault.initialize_key(key);vault.initialize_key(backup_key)
    monkeypatch.setenv('OVD_CREDENTIAL_KEY_FILE',str(key));monkeypatch.setenv('OVD_REQUIRE_ENCRYPTION','1')
    security.write_owner(config,{'password_hash':security.password_hash(PASSWORD),'totp_secret':SECRET})
    store=Store(state/'library.sqlite3');accounts.import_cookies(COOKIES,config,store,member_info())
    item=library/('a'*32)/'fixture.mp4';item.parent.mkdir();item.write_bytes(b'synthetic-media-fixture')
    store.put('library','a'*32,{'status':'completed','path':str(item),'metadata':{'title':'Fixture'}})
    with store.db:store.db.execute('INSERT INTO sessions VALUES(?,?)',('synthetic-session-hash',time.time()+100))
    store.put('reauth','fixture',{'expires':time.time()+100});store.close()
    return config,state,library,backup_key,item


def test_streaming_backup_restore_integrity_key_separation_and_session_revocation(tmp_path,monkeypatch):
    config,state,library,key,item=dataset(tmp_path,monkeypatch);archive=tmp_path/'backups/snapshot.ovdbak'
    recovery.backup(config,state,library,archive,key);raw=archive.read_bytes()
    assert raw.startswith(recovery.HEADER) and b'synthetic' not in raw and SECRET.encode() not in raw
    restored=tmp_path/'restored';recovery.restore(archive,restored,key)
    assert (restored/'library'/item.relative_to(library)).read_bytes()==item.read_bytes()
    validate=Store(restored/'state/library.sqlite3')
    assert validate.get('library','a'*32)['path']==str(restored/'library'/item.relative_to(library))
    assert validate.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0 and not validate.all('reauth')
    assert accounts.worker_credentials(URL,'youtube',validate,restored/'config')['cookie_text']==COOKIES
    assert security.owner_config(restored/'config')['totp_secret']==SECRET;validate.close()
    assert not list((tmp_path/'backups').glob('ovd-backup-*')) and not list(tmp_path.glob('ovd-restore-*'))
    with pytest.raises(RuntimeError):recovery.restore(archive,restored,key)


@pytest.mark.parametrize('failure',['wrong_backup_key','tamper','truncated','wrong_master_key'])
def test_recovery_fails_closed_without_publishing_data(tmp_path,monkeypatch,failure):
    config,state,library,key,item=dataset(tmp_path,monkeypatch);archive=tmp_path/'snapshot.ovdbak'
    recovery.backup(config,state,library,archive,key)
    if failure=='wrong_backup_key':key.write_bytes(os.urandom(32))
    elif failure=='wrong_master_key':Path(os.environ['OVD_CREDENTIAL_KEY_FILE']).write_bytes(os.urandom(32))
    elif failure=='tamper':raw=bytearray(archive.read_bytes());raw[25]^=1;archive.write_bytes(raw)
    else:archive.write_bytes(archive.read_bytes()[:-20])
    with pytest.raises(vault.VaultError):recovery.restore(archive,tmp_path/'restored',key)
    assert not (tmp_path/'restored').exists() and item.read_bytes()==b'synthetic-media-fixture'
    assert not list(tmp_path.glob('ovd-restore-*'))


def test_backup_and_rotation_refuse_running_application(tmp_path,monkeypatch):
    config,state,library,key,item=dataset(tmp_path,monkeypatch)
    with recovery.lock_state(state):
        with pytest.raises(RuntimeError,match='Stop'):recovery.backup(config,state,library,tmp_path/'snapshot.ovdbak',key)
    assert not (tmp_path/'snapshot.ovdbak').exists()


def test_fifo_key_is_rejected_without_blocking(client,tmp_path):
    fifo=tmp_path/'fifo.key';os.mkfifo(fifo,0o600)
    with pytest.raises(vault.VaultError):vault.read_key(fifo)


def test_backup_space_guard_preserves_source(tmp_path,monkeypatch):
    config,state,library,key,item=dataset(tmp_path,monkeypatch)
    from collections import namedtuple
    usage=namedtuple('usage','total used free')
    monkeypatch.setattr(recovery.shutil,'disk_usage',lambda path:usage(1024,1023,1))
    with pytest.raises(RuntimeError,match='Insufficient backup space'):
        recovery.backup(config,state,library,tmp_path/'snapshot.ovdbak',key)
    assert not (tmp_path/'snapshot.ovdbak').exists() and item.read_bytes()==b'synthetic-media-fixture'


def test_staged_key_rotation_preserves_original_and_metadata(tmp_path,monkeypatch):
    config,state,library,key,item=dataset(tmp_path,monkeypatch);new=tmp_path/'new-key/master.key';vault.initialize_key(new)
    original=(config/'owner.json').read_bytes();out=tmp_path/'rekeyed'
    recovery.rekey_config(config,state,out,new)
    assert (config/'owner.json').read_bytes()==original and (out/'owner.json').read_bytes()!=original
    monkeypatch.setenv('OVD_CREDENTIAL_KEY_FILE',str(new))
    assert security.owner_config(out)['totp_secret']==SECRET
    store=Store(state/'library.sqlite3');assert accounts.worker_credentials(URL,'youtube',store,out)['cookie_text']==COOKIES;store.close()


def test_backup_chunk_boundaries_truncation_reordering_and_size_limits(tmp_path):
    key=os.urandom(32);original=os.urandom(recovery.CHUNK*2+19)
    out=io.BytesIO();writer=recovery.EncryptedWriter(out,key);writer.write(original);writer.finish()
    source=tmp_path/'chunks.ovdbak';source.write_bytes(out.getvalue())
    recovery.decrypt_archive(source,tmp_path/'valid',key,len(original))
    assert (tmp_path/'valid').read_bytes()==original
    with pytest.raises(vault.VaultError):recovery.decrypt_archive(source,tmp_path/'limited',key,100)
    raw=out.getvalue();first=raw[16:16+recovery.CHUNK+20];second=raw[16+recovery.CHUNK+20:16+2*(recovery.CHUNK+20)]
    source.write_bytes(raw[:16]+second+first+raw[16+2*(recovery.CHUNK+20):])
    with pytest.raises(vault.VaultError):recovery.decrypt_archive(source,tmp_path/'reordered',key,len(original))


@pytest.mark.parametrize('unsafe',['../escape','/absolute','library/link'])
def test_authenticated_unsafe_archive_is_refused(tmp_path,monkeypatch,unsafe):
    import tarfile
    config,state,library,key,item=dataset(tmp_path,monkeypatch);archive=tmp_path/'unsafe.ovdbak'
    with open(archive,'wb') as stream:
        writer=recovery.EncryptedWriter(stream,vault.read_key(key))
        with tarfile.open(fileobj=writer,mode='w|gz') as tar:
            entry=tarfile.TarInfo(unsafe)
            if unsafe.endswith('link'):entry.type=tarfile.SYMTYPE;entry.linkname='/outside'
            tar.addfile(entry)
        writer.finish()
    with pytest.raises(RuntimeError,match='Unsafe'):recovery.restore(archive,tmp_path/'restored',key)
    assert not (tmp_path/'restored').exists() and item.exists()
