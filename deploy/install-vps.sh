#!/bin/bash
# Run as root on the Ubuntu VPS; never on the Mac.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo on the VPS.' >&2; exit 2; }
# shellcheck disable=SC1091
. /etc/os-release
[ "$ID" = ubuntu ] && [[ "$VERSION_ID" =~ ^(22\.04|24\.04|26\.04)$ ]] || { echo 'Supported: Ubuntu 22.04/24.04/26.04 LTS.' >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl python3 iptables ufw openssh-server
# Preserve existing installations. Refuse incompatible package replacement.
if ! command -v docker >/dev/null; then
  for package in docker.io podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
      echo "Conflicting package $package exists; review before replacing it." >&2; exit 2
    fi
  done
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
major="$(docker version --format '{{.Server.Version}}' | cut -d. -f1)"
[ "$major" -ge 28 ] || { echo 'Docker Engine 28+ is required for safe localhost publication.' >&2; exit 2; }
docker compose version
if ! command -v tailscale >/dev/null; then
  installer="$(mktemp)"
  curl -fsSL https://tailscale.com/install.sh -o "$installer"
  sh "$installer"
  rm -f "$installer"
fi
systemctl enable --now tailscaled
# Only configure SSH/firewall using the actual established SSH session.
[ -n "${OVD_SSH_CONNECTION:-}" ] || { echo 'Missing verified SSH session; firewall was not changed.' >&2; exit 2; }
read -r _client _client_port _server ssh_port <<< "$OVD_SSH_CONNECTION"
[[ "$ssh_port" =~ ^[0-9]+$ ]] || exit 2
ufw allow "$ssh_port/tcp" comment 'Preserve current SSH access'
ufw insert 1 deny 8000/tcp comment 'Downloader accessible through local Serve proxy only'
ufw allow in on tailscale0 comment 'Authenticated private tailnet traffic'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
echo "Firewall: allow SSH TCP $ssh_port and tailnet; deny public 8000; default deny incoming."
# No password-auth changes until the operator verifies a working key login.
if ! tailscale status --json | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin).get("BackendState") != "Running")'; then
  echo 'Authorize this VPS using the URL shown below. No reusable auth key is stored.'
  tailscale up --timeout=60s || true
  if ! tailscale status --json | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin).get("BackendState") != "Running")'; then
    echo 'Authentication is pending. Open the authorization URL, then rerun the same upload/install command.' >&2
    exit 20
  fi
fi
