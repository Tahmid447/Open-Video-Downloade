"""Local/server maintenance CLI. Do not pass credentials as command-line arguments."""
import argparse
import getpass
import json
import os
from pathlib import Path
from database import Store
from security import password_hash

parser=argparse.ArgumentParser()
parser.add_argument('command',choices=['set-password','backup-db'])
parser.add_argument('--config', default=os.getenv('CONFIG_DIR','/data/config'))
parser.add_argument('--state', default=os.getenv('STATE_DIR','/data/state'))
parser.add_argument('--output')
args=parser.parse_args()
if args.command=='set-password':
    password=getpass.getpass('New owner password (12+ characters): ')
    if len(password)<12 or password!=getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords must match and contain at least 12 characters.')
    directory=Path(args.config); directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=directory/'owner.json'; temporary=directory/'owner.json.new'
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f: json.dump({'password_hash':password_hash(password)},f)
    os.replace(temporary,target)
    print('Owner password saved. Existing sessions are invalidated.')
else:
    if not args.output: raise SystemExit('--output is required; use a private backup location.')
    store=Store(Path(args.state)/'library.sqlite3'); store.backup(args.output); store.close()
    print('Consistent database backup saved. Back up media and config separately.')
