#!/bin/bash
# Dedicated Ubuntu ARM64 host only. Never formats disks or substitutes a missing mount.
set -euo pipefail
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = aarch64 ] && [ "$(id -u)" -eq 0 ] || { echo 'Requires sudo on the Ubuntu ARM64 server.' >&2; exit 2; }
mountpoint -q /srv/ovd-data || { echo 'Mount the approved block volume at /srv/ovd-data first. No boot-disk fallback.' >&2; exit 2; }
expected=/etc/open-video-downloader/data-volume.uuid
[ -s "$expected" ] || { echo 'Record the approved block filesystem UUID before installation.' >&2; exit 2; }
actual="$(findmnt -rn -o UUID -T /srv/ovd-data)"
[ "$actual" = "$(cat "$expected")" ] && [ -n "$actual" ] || { echo 'Data-volume UUID mismatch. Refusing deployment.' >&2; exit 2; }
install -d -m 0700 -o 10001 -g 10001 /srv/ovd-data/library
install -d -m 0700 -o 10001 -g 10001 /srv/ovd-data/backups
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
