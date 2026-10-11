#!/bin/bash
# Runs only through approved OCI cloud-init on a new, dedicated Ubuntu ARM64 VM.
# Source SHA and SSH CIDR are supplied privately by the guarded launcher.
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] && [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = aarch64 ]
source /etc/ovd-bootstrap.conf
[[ "$OVD_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
install -d -m 0700 /var/lib/ovd-bootstrap /etc/open-video-downloader
exec 9>/var/lib/ovd-bootstrap/lock
flock -n 9 || exit 0
# Keep SSH fingerprints in the authenticated OCI console, never a public CI log.
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print "OVD_HOST_ED25519_FINGERPRINT=" $2}' >/dev/console
if [ -f /var/lib/ovd-bootstrap/ready ]; then
  /opt/open-video-downloader/current/deploy/verify-https-private.sh
  test "$(cat /srv/ovd-data/state/bootstrap-persistence-check)" = "$OVD_SOURCE_SHA"
  release=/opt/open-video-downloader/current
  docker compose --project-name open-video-downloader --env-file "$release/deploy/.env" --env-file "$release/deploy/image.env" -f "$release/deploy/docker-compose.yml" exec -T downloader python -c 'from database import Store; s=Store("/data/state/library.sqlite3"); assert s.get("deployment","persistence-probe")["verified"] is True; assert s.db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; s.close()' 
  printf 'OVD_PRIVATE_READY_BOOT=%s\n' "$(cat /proc/sys/kernel/random/boot_id)" >/dev/console
  exit 0
fi
# The launcher attaches exactly its tagged, newly created 150 GB volume at vdb.
device=/dev/oracleoci/oraclevdb
for attempt in $(seq 1 90); do
  [ -b "$device" ] && break
  sleep 10
done
[ -b "$device" ] || { echo 'Approved data device is not attached.' >&2; exit 2; }
if [ ! -f /etc/open-video-downloader/data-volume.uuid ]; then
  python3 - "$device" <<'PY'
import json,subprocess,sys
path=sys.argv[1]
d=json.loads(subprocess.check_output(['lsblk','--json','--bytes','--output','PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS',path]))['blockdevices']
assert len(d)==1
v=d[0]
assert v['type']=='disk' and 150*10**9 <= int(v['size']) <= 150*1024**3
assert not v.get('fstype') and not any(v.get('mountpoints') or []) and not v.get('children')
signatures=json.loads(subprocess.check_output(['wipefs','--no-act','--json',path])).get('signatures',[])
assert not signatures, 'Unknown existing filesystem: never format it.'
PY
  mkfs.ext4 -q -m 1 -L ovd-data "$device"
  blkid -s UUID -o value "$device" > /etc/open-video-downloader/data-volume.uuid
  chmod 0600 /etc/open-video-downloader/data-volume.uuid
fi
uuid="$(cat /etc/open-video-downloader/data-volume.uuid)"
[[ "$uuid" =~ ^[a-f0-9-]{36}$ ]]
[ "$(blkid -s UUID -o value "$device")" = "$uuid" ]
install -d -m 0700 /srv/ovd-data
if ! grep -Fq "UUID=$uuid /srv/ovd-data " /etc/fstab; then
  # No nofail: Docker and the app must fail closed when this volume is missing.
  printf 'UUID=%s /srv/ovd-data ext4 defaults,nodev,nosuid,noexec 0 2\n' "$uuid" >> /etc/fstab
fi
mountpoint -q /srv/ovd-data || mount /srv/ovd-data
[ "$(findmnt -n -o UUID --target /srv/ovd-data)" = "$uuid" ]
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl python3
stage="$(mktemp -d /tmp/ovd-upload-XXXXXXXX)"
archive="$(mktemp /tmp/ovd-source-XXXXXXXX.tar.gz)"
trap 'rm -f "$archive"; rm -rf "$stage"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "https://codeload.github.com/Tahmid447/Open-Video-Downloade/tar.gz/$OVD_SOURCE_SHA" -o "$archive"
# codeload serves the immutable reviewed commit; reject dangerous archive entries.
python3 - "$archive" "$stage" <<'PY'
import pathlib,sys,tarfile
with tarfile.open(sys.argv[1]) as archive:
    members=archive.getmembers()
    for member in members:
        p=pathlib.PurePosixPath(member.name)
        assert not p.is_absolute() and '..' not in p.parts
        assert member.isfile() or member.isdir()
        member.name='/'.join(p.parts[1:])
    archive.extractall(sys.argv[2],members=[m for m in members if m.name],filter='data')
PY
# Set a measured initial library budget, preserving 16 GiB for temporary work,
# state, small recovery rehearsals and free-space safeguards. No paid expansion.
install -d -m 0700 /opt/open-video-downloader/shared
if [ ! -f /opt/open-video-downloader/shared/.env ]; then
  python3 - "$stage" <<'PY'
import json,os,pathlib,sys
stage=pathlib.Path(sys.argv[1]);s=os.statvfs('/srv/ovd-data');unit=1024**3
available=s.f_bavail*s.f_frsize
budget=(available//unit-16)*unit
assert budget>=100*unit
text=(stage/'deploy/.env.example').read_text()
text=text.replace('MAX_LIBRARY_BYTES=107374182400','MAX_LIBRARY_BYTES='+str(budget))
p=pathlib.Path('/opt/open-video-downloader/shared/.env');p.write_text(text);p.chmod(0o600)
pathlib.Path('/var/lib/ovd-bootstrap/storage-budget.json').write_text(json.dumps({'filesystem_available_bytes':available,'library_limit_bytes':budget,'reserved_headroom_bytes':available-budget}))
PY
fi
export OVD_BOOTSTRAP_SSH_CIDR
"$stage/deploy/apply-https-release.sh" "$stage" install
release=/opt/open-video-downloader/current
compose=(docker compose --project-name open-video-downloader --env-file "$release/deploy/.env" --env-file "$release/deploy/image.env" -f "$release/deploy/docker-compose.yml")
"${compose[@]}" exec -T downloader python -c 'from database import Store; s=Store("/data/state/library.sqlite3"); s.put("deployment","persistence-probe",{"verified":True}); s.close()'
"${compose[@]}" up -d --no-build --force-recreate --wait --wait-timeout 90
"$release/deploy/verify-https-private.sh"
"${compose[@]}" exec -T downloader python -c 'from database import Store; s=Store("/data/state/library.sqlite3"); assert s.get("deployment","persistence-probe")["verified"] is True; assert s.db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; s.close()'
printf '%s\n' "$OVD_SOURCE_SHA" > /srv/ovd-data/state/bootstrap-persistence-check
chown 10001:10001 /srv/ovd-data/state/bootstrap-persistence-check
printf '%s\n' "$OVD_SOURCE_SHA" > /var/lib/ovd-bootstrap/ready
printf 'OVD_PRIVATE_READY_BOOT=%s\n' "$(cat /proc/sys/kernel/random/boot_id)" >/dev/console
