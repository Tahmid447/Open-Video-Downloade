#!/bin/bash
# Mac -> VPS. Uses a strict source whitelist, key SSH and root-owned remote config.
set -euo pipefail
SERVER="${1:?Usage: ./deploy/update-vps.sh user@server [--install]}"
MODE="${2:-}"
[[ "$SERVER" =~ ^([a-zA-Z0-9_.-]+@)?[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo 'Use a hostname/IP or user@host (configure custom SSH port in ~/.ssh/config).' >&2; exit 2; }
[ -z "$MODE" ] || [ "$MODE" = --install ] || exit 2
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="$(mktemp -t ovd-upload.XXXXXX)"
REMOTE="/tmp/ovd-upload-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
trap 'rm -f "$ARCHIVE"' EXIT
# No .venv, media, logs, LaunchAgent or local environment/secrets are transferred.
files=(app.py common.py worker.py network_guard.py database.py extraction.py media.py security.py accounts.py manage.py vault.py recovery.py requirements.txt Dockerfile .dockerignore static templates
       deploy/docker-compose.yml deploy/.env.example deploy/healthcheck.sh deploy/service.sh
       deploy/install-vps.sh deploy/apply-release.sh deploy/egress-firewall.sh deploy/verify-vps.sh deploy/README-VPS.md)
tar -czf "$ARCHIVE" -C "$ROOT" "${files[@]}"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER" "umask 077; mkdir '$REMOTE'"
scp -o BatchMode=yes -o ConnectTimeout=10 "$ARCHIVE" "$SERVER:$REMOTE/source.tar.gz"
ssh -o BatchMode=yes "$SERVER" "tar -xzf '$REMOTE/source.tar.gz' -C '$REMOTE'; rm '$REMOTE/source.tar.gz'"
remote_mode="${MODE#--}"
# TTY permits sudo authentication and displays Tailscale's one-time browser URL.
# The only remotely interpolated values are validated/generated above.
if ssh -t -o BatchMode=yes "$SERVER" "sudo env OVD_SSH_CONNECTION=\"\$SSH_CONNECTION\" bash '$REMOTE/deploy/apply-release.sh' '$REMOTE' '$remote_mode'"; then
  ssh -o BatchMode=yes "$SERVER" "rm -rf '$REMOTE'"
  echo 'SUCCESS: private VPS downloader deployed and healthy.'
else
  status=$?
  ssh -o BatchMode=yes "$SERVER" "rm -rf '$REMOTE'" || true
  echo 'Deployment did not complete. See the error above. Authenticate Tailscale if requested, then rerun.' >&2
  exit "$status"
fi
