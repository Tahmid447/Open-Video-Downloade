#!/bin/bash
# FINAL public exposure step. Run only after the human approves this exact hostname/config.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo on the dedicated VPS.' >&2; exit 2; }
[ "${1:-}" = --approved ] || { echo 'Requires explicit final exposure approval: --approved hostname' >&2; exit 2; }
hostname="${2:?Selected hostname required}"
[[ "$hostname" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || { echo 'Use a lowercase DNS hostname.' >&2; exit 2; }
RELEASE="$(readlink -f /opt/open-video-downloader/current)"
[ -d "$RELEASE" ] || exit 2
"$RELEASE/deploy/verify-https-private.sh"
compose=(docker compose --project-name open-video-downloader --env-file "$RELEASE/deploy/.env" --env-file "$RELEASE/deploy/image.env" -f "$RELEASE/deploy/docker-compose.yml")
"${compose[@]}" exec -T downloader python -c 'from security import owner_config; d=owner_config("/data/config") or {}; assert d.get("password_hash") and d.get("totp_secret"), "Owner password and MFA must be configured first"'
# No session/cookie contents enter the release or web root.
sed -i '/^OVD_ALLOWED_HOSTS=/d' /opt/open-video-downloader/shared/.env
printf 'OVD_ALLOWED_HOSTS=%s,127.0.0.1,localhost\n' "$hostname" >> /opt/open-video-downloader/shared/.env
"${compose[@]}" up -d --no-build --wait --wait-timeout 90
printf 'OVD_HOSTNAME=%s\n' "$hostname" > /etc/open-video-downloader/caddy.env
chmod 0600 /etc/open-video-downloader/caddy.env
install -d -m 0755 /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/ovd.conf <<'UNIT'
[Unit]
Requires=open-video-downloader.service
After=open-video-downloader.service
[Service]
EnvironmentFile=/etc/open-video-downloader/caddy.env
UNIT
cp -p /etc/caddy/Caddyfile /opt/open-video-downloader/backups/Caddyfile-before-exposure
install -m 0644 "$RELEASE/deploy/Caddyfile.example" /etc/caddy/Caddyfile
OVD_HOSTNAME="$hostname" caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
fail_closed() {
  status=$?
  trap - ERR
  systemctl disable --now caddy || true
  ufw insert 1 deny 80/tcp || true
  ufw insert 1 deny 443/tcp || true
  echo 'Exposure verification failed. Proxy stopped and public HTTP(S) denied.' >&2
  exit "$status"
}
trap fail_closed ERR
# OCI ingress 80/443 is a separate operator-reviewed change. Never open 8000.
ufw --force delete deny 80/tcp
ufw --force delete deny 443/tcp
ufw allow 80/tcp comment 'Caddy ACME and HTTPS redirects'
ufw allow 443/tcp comment 'Owner-authenticated HTTPS'
systemctl enable --now caddy
curl --retry 12 --retry-delay 5 --retry-all-errors --connect-timeout 5 --max-time 10 -fsS "https://$hostname/api/health" >/dev/null
[ "$(curl -s -o /dev/null -w '%{http_code}' "https://$hostname/api/library")" = 401 ]
trap - ERR
echo "HTTPS active: https://$hostname/ . Verify external IPv4/IPv6, MFA, reboot and Mac-off access."
