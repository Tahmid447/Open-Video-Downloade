#!/bin/bash
# Mac -> VPS. Uses a strict source whitelist, key SSH and root-owned remote config.
set -euo pipefail
SERVER="${1:?Usage: ./deploy/update-https-vps.sh user@server [--install]}"
MODE="${2:-}"
[[ "$SERVER" =~ ^([a-zA-Z0-9_.-]+@)?[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo 'Use a hostname/IP or user@host (configure custom SSH port in ~/.ssh/config).' >&2; exit 2; }
[ -z "$MODE" ] || [ "$MODE" = --install ] || exit 2
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="$(mktemp -t ovd-upload.XXXXXX)"
REMOTE="/tmp/ovd-upload-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
trap 'rm -f "$ARCHIVE"' EXIT
# No .venv, media, logs, LaunchAgent or local environment/secrets are transferred.
files=(app.py common.py worker.py network_guard.py database.py extraction.py media.py security.py accounts.py manage.py vault.py recovery.py requirements.txt Dockerfile .dockerignore static templates
       deploy/docker-compose.yml deploy/.env.example deploy/healthcheck.sh deploy/service.sh deploy/Caddyfile.example deploy/enable-https.sh deploy/README-HTTPS.md
       deploy/prepare-cloud-storage.sh deploy/verify-data-mount.sh deploy/verify-storage-volumes.py deploy/install-https-base.sh deploy/apply-https-release.sh deploy/egress-firewall.sh deploy/verify-https-private.sh deploy/README-VPS.md deploy/ACCOUNT-CONNECTIONS.md deploy/ORACLE-CHECKLIST.md)
# Directory allowlisting alone could include ignored local files. Transfer reviewed,
# committed files only; never recursively walk a runtime or asset directory.
if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo 'Commit reviewed source changes before deployment.' >&2; exit 2
fi
tracked=()
while IFS= read -r -d '' file; do
  [ -f "$ROOT/$file" ] && [ ! -L "$ROOT/$file" ] || { echo 'Refusing missing/symlinked source.' >&2; exit 2; }
  tracked+=("$file")
done < <(git -C "$ROOT" ls-files -z -- "${files[@]}")
[ "${#tracked[@]}" -gt 20 ] || { echo 'Incomplete reviewed Git source.' >&2; exit 2; }
tar -czf "$ARCHIVE" -C "$ROOT" "${tracked[@]}"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER" "umask 077; mkdir '$REMOTE'"
scp -o BatchMode=yes -o ConnectTimeout=10 "$ARCHIVE" "$SERVER:$REMOTE/source.tar.gz"
ssh -o BatchMode=yes "$SERVER" "tar -xzf '$REMOTE/source.tar.gz' -C '$REMOTE'; rm '$REMOTE/source.tar.gz'"
remote_mode="${MODE#--}"
# TTY permits interactive sudo authentication.
# The only remotely interpolated values are validated/generated above.
if ssh -t -o BatchMode=yes "$SERVER" "sudo env OVD_SSH_CONNECTION=\"\$SSH_CONNECTION\" bash '$REMOTE/deploy/apply-https-release.sh' '$REMOTE' '$remote_mode'"; then
  ssh -o BatchMode=yes "$SERVER" "rm -rf '$REMOTE'"
  echo 'SUCCESS: private VPS downloader deployed and healthy.'
else
  status=$?
  ssh -o BatchMode=yes "$SERVER" "rm -rf '$REMOTE'" || true
  echo 'Deployment did not complete. See the error above. Resolve the reported private preparation error, then rerun.' >&2
  exit "$status"
fi
