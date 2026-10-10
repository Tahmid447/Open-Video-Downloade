#!/bin/bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo.' >&2; exit 2; }
STAGE="$(cd "${1:?staging directory required}" && pwd -P)"
[[ "$STAGE" =~ ^/tmp/ovd-upload-[a-zA-Z0-9-]+$ ]] || { echo 'Unexpected staging path' >&2; exit 2; }
BASE=/opt/open-video-downloader
install -d -m 0700 "$BASE" "$BASE/releases" "$BASE/backups" "$BASE/shared"
exec 9>"$BASE/deployment.lock"
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 2; }
if [ "${2:-}" = install ]; then
  "$STAGE/deploy/install-vps.sh"
fi
command -v docker >/dev/null && command -v tailscale >/dev/null
major="$(docker version --format '{{.Server.Version}}' | cut -d. -f1)"
[ "$major" -ge 28 ] || { echo 'Docker Engine 28+ is required.' >&2; exit 2; }
# Refuse an unexpected port or an already-enabled public sharing configuration.
[ ! -f "$BASE/shared/.env" ] || ! grep -Eq '^(OVD_PORT|OVD_IMAGE|DOWNLOAD_DIR|LIBRARY_DIR|STATE_DIR|CONFIG_DIR|COOKIE_SECURE|OVD_ALLOW_UNAUTHENTICATED_LOCAL|LIBRARY_REQUIRE_MOUNT)=' "$BASE/shared/.env" || { echo 'Reserved settings in shared .env; remove them before deployment.' >&2; exit 2; }
tailscale serve status --json | python3 -c '
import json,sys
c=json.load(sys.stdin)
def walk(o):
    if isinstance(o,dict):
        for k,v in o.items():
            if k=="AllowFunnel" and isinstance(v,dict) and any(v.values()): raise SystemExit("Public sharing exists. Inspect it before deploying this private service.")
            if k in {"Path", "Text", "TCPForward"}: raise SystemExit("An existing Serve handler would be replaced. Inspect it first.")
            if k=="Proxy" and v != "http://127.0.0.1:8000": raise SystemExit("An existing Serve proxy would be replaced. Inspect it first.")
            walk(v)
    elif isinstance(o,list):
        for v in o: walk(v)
walk(c)
'
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
RELEASE="$BASE/releases/$release_id"
cp -R "$STAGE" "$RELEASE"
chown -R root:root "$RELEASE"
chmod -R go-w "$RELEASE"
if [ ! -f "$BASE/shared/.env" ]; then
  install -m 0600 "$RELEASE/deploy/.env.example" "$BASE/shared/.env"
fi
cp -p "$BASE/shared/.env" "$BASE/backups/config-$release_id.env"
ln -s "$BASE/shared/.env" "$RELEASE/deploy/.env"
printf 'OVD_IMAGE=open-video-downloader:%s\n' "$release_id" > "$RELEASE/deploy/image.env"
compose=(docker compose --project-name open-video-downloader --env-file "$RELEASE/deploy/.env" --env-file "$RELEASE/deploy/image.env" -f "$RELEASE/deploy/docker-compose.yml")
"${compose[@]}" config --quiet
# Build while the old container is still serving. A failed build leaves it intact.
"${compose[@]}" build --pull
install -d -m 0755 /etc/open-video-downloader /etc/systemd/system/docker.service.d
install -m 0755 "$RELEASE/deploy/egress-firewall.sh" /etc/open-video-downloader/egress-firewall.sh
/etc/open-video-downloader/egress-firewall.sh
cat > /etc/systemd/system/docker.service.d/ovd-firewall.conf <<'EOF'
[Service]
ExecStartPre=/etc/open-video-downloader/egress-firewall.sh pre
ExecStartPost=/etc/open-video-downloader/egress-firewall.sh
EOF
OLD="$(readlink -f "$BASE/current" 2>/dev/null || true)"
rollback() {
  status=$?
  trap - ERR
  echo 'Deployment failed; restoring previous image and current release.' >&2
  if [ -n "$OLD" ] && [ -d "$OLD" ]; then
    rm -f "$BASE/current.new"
    ln -s "$OLD" "$BASE/current.new"
    mv -Tf "$BASE/current.new" "$BASE/current"
    "$OLD/deploy/service.sh" start || echo 'Rollback failed; inspect Docker logs.' >&2
  else
    "${compose[@]}" stop || true
  fi
  exit "$status"
}
trap rollback ERR
# Quiesce the old scheduler after the new image builds, then take a WAL-safe DB snapshot.
if [ -n "$OLD" ] && [ -d "$OLD" ]; then
  "$OLD/deploy/service.sh" stop
fi
"${compose[@]}" run --rm --no-deps -T --entrypoint python downloader -c '
import pathlib
from database import Store
p=pathlib.Path("/data/state/library.sqlite3")
if p.exists():
    s=Store(p); s.backup("/data/state/pre-update.sqlite3"); s.close()
'
# Copy the state backup out of its volume. Media and session config are never replaced.
"${compose[@]}" run --rm --no-deps -T --entrypoint python downloader -c '
import pathlib,sys
p=pathlib.Path("/data/state/pre-update.sqlite3")
if p.exists(): sys.stdout.buffer.write(p.read_bytes())
' > "$BASE/backups/database-$release_id.sqlite3"
chmod 0600 "$BASE/backups/database-$release_id.sqlite3"
"$RELEASE/deploy/service.sh" start
# Installed CLI is authoritative; do not assume a legacy Serve command.
tailscale serve --help | grep -q -- '--bg' || { echo 'Unsupported Tailscale Serve CLI.' >&2; exit 2; }
timeout 60s tailscale serve --bg --https=443 http://127.0.0.1:8000 || {
  echo 'Private HTTPS setup did not complete. Follow any Tailscale authorization URL above, then rerun.' >&2
  false
}
"$RELEASE/deploy/verify-vps.sh"
rm -f "$BASE/current.new"
ln -s "$RELEASE" "$BASE/current.new"
mv -Tf "$BASE/current.new" "$BASE/current"
cat > /etc/systemd/system/open-video-downloader.service <<'EOF'
[Unit]
Description=Private Open Video Downloader
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/open-video-downloader/current/deploy/service.sh start
ExecStop=/opt/open-video-downloader/current/deploy/service.sh stop
TimeoutStartSec=120
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable open-video-downloader
# Service startup manages only this Compose project; it never changes Mac services.
systemctl start open-video-downloader
trap - ERR
# Keep the current + three previous releases/config backups. Image cache is bounded.
python3 - "$BASE" <<'PY'
import pathlib, shutil, subprocess, sys
base=pathlib.Path(sys.argv[1]); current=(base/'current').resolve()
releases=sorted((base/'releases').iterdir(),key=lambda p:p.name,reverse=True)
keep={current,*releases[:4]}
for path in releases:
    if path not in keep:
        subprocess.run(['docker','image','rm','open-video-downloader:'+path.name],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.rmtree(path)
for path in sorted((base/'backups').glob('config-*.env'),reverse=True)[4:]: path.unlink()
PY
# Build cache otherwise grows on each future update; prune only this builder's old cache.
# No automatic global prune: unrelated projects may depend on that cache.
echo 'Deployment succeeded. Persistent library, state and session volumes were preserved.'
echo 'For initial owner setup: docker compose exec downloader python manage.py set-password (use the current release Compose files).'
tailscale serve status
