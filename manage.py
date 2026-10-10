"""Local/server maintenance CLI. Do not pass credentials as command-line arguments."""
import argparse
import asyncio
import base64
import sys
import time
import getpass
import json
import os
from pathlib import Path
from database import Store
from security import password_hash, write_owner, owner_config, check_password, verify_totp

parser=argparse.ArgumentParser()
parser.add_argument('command',choices=['set-password','enroll-mfa','auth-status','backup-db'])
parser.add_argument('--config', default=os.getenv('CONFIG_DIR','/data/config'))
parser.add_argument('--state', default=os.getenv('STATE_DIR','/data/state'))
parser.add_argument('--output')
args=parser.parse_args()
if args.command=='set-password':
    password=getpass.getpass('New owner password (12+ characters): ')
    if len(password)<12 or password!=getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords must match and contain at least 12 characters.')
    write_owner(args.config,{'password_hash':password_hash(password)})
    print('Owner password saved. Existing sessions and MFA are reset. Re-enroll MFA before public exposure.')
elif args.command=='enroll-mfa':
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('Use an interactive private terminal; do not pipe or record the authenticator secret.')
    password=getpass.getpass('Owner password: ')
    asyncio.run(check_password(password,args.config))
    data=owner_config(args.config)
    if data.get('totp_secret'):raise SystemExit('MFA already enabled. Use set-password only if server recovery is required.')
    secret=base64.b32encode(os.urandom(20)).decode()
    print('Add manually to your authenticator: OpenVideoDownloader / Owner')
    print('Private setup key:',secret)
    data['totp_secret']=secret
    code=getpass.getpass('Six-digit authenticator code: ')
    if not verify_totp(data,code):raise SystemExit('Incorrect code. No MFA changes saved; run enrollment again.')
    write_owner(args.config,data)
    print('MFA enabled. Previous browser sessions are invalidated.')
elif args.command=='auth-status':
    data=owner_config(args.config) or {}
    print(json.dumps({'configured':bool(data.get('password_hash')),'mfa_enabled':bool(data.get('totp_secret'))}))
else:
    if not args.output: raise SystemExit('--output is required; use a private backup location.')
    store=Store(Path(args.state)/'library.sqlite3'); store.backup(args.output); store.close()
    print('Consistent database backup saved. Back up media and config separately.')
