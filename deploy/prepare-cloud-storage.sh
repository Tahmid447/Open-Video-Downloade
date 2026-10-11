#!/bin/bash
# Dedicated Ubuntu ARM64 host only. Never formats disks or substitutes a missing mount.
set -euo pipefail
bash "$(dirname "$0")/verify-data-mount.sh"
# Docker retains old volume options when Compose changes. Never silently use an
# existing boot-disk volume or replace it with an empty state directory.
if command -v docker >/dev/null; then
  python3 "$(dirname "$0")/verify-storage-volumes.py"
fi
for directory in downloads library state config backups; do
  [ ! -L "/srv/ovd-data/$directory" ] || { echo 'Symlinked data directory. Refusing deployment.' >&2; exit 2; }
  install -d -m 0700 -o 10001 -g 10001 "/srv/ovd-data/$directory"
done
install -d -m 0700 -o 10001 -g 10001 /etc/open-video-downloader/keys/provider
key=/etc/open-video-downloader/keys/provider/master.key
if [ ! -e "$key" ]; then
  python3 - "$key" <<'PY'
import os,sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'wb') as f:f.write(os.urandom(32));f.flush();os.fsync(f.fileno())
PY
  chown 10001:10001 "$key"
fi
python3 - "$key" <<'PY'
import os,stat,sys
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
with os.fdopen(fd,'rb') as f:
    s=os.fstat(f.fileno());assert stat.S_ISREG(s.st_mode) and s.st_mode&0o777==0o600 and s.st_uid==10001 and len(f.read(33))==32
PY
echo 'Approved block mount and separate provider key verified; key contents were not displayed.'
