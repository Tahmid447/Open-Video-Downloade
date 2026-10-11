#!/bin/bash
# Run as root on the Ubuntu VPS; never on the Mac.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo on the VPS.' >&2; exit 2; }
# shellcheck disable=SC1091
. /etc/os-release
[ "$ID" = ubuntu ] && [[ "$VERSION_ID" =~ ^(22\.04|24\.04|26\.04)$ ]] || { echo 'Supported: Ubuntu 22.04/24.04/26.04 LTS.' >&2; exit 2; }
if command -v caddy >/dev/null && systemctl is-active --quiet caddy; then
  echo 'An existing Caddy service is active. Refusing to stop an unrelated site.' >&2; exit 2
fi
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

if [ -n "${OVD_BOOTSTRAP_SSH_CIDR:-}" ]; then
  # Headless initial installation: an explicit, owner-approved IPv4 /32 only.
  python3 - "$OVD_BOOTSTRAP_SSH_CIDR" <<'PYCODE'
import ipaddress,sys
n=ipaddress.ip_network(sys.argv[1],strict=True)
assert n.version==4 and n.prefixlen==32 and n.network_address.is_global
PYCODE
  ufw allow from "$OVD_BOOTSTRAP_SSH_CIDR" to any port 22 proto tcp comment 'Approved owner SSH only'
else
  [ -n "${OVD_SSH_CONNECTION:-}" ] || { echo 'Missing verified SSH session; no firewall changes.' >&2; exit 2; }
  read -r _client _client_port _server ssh_port <<< "$OVD_SSH_CONNECTION"
  [[ "$ssh_port" =~ ^[0-9]+$ ]] || exit 2
  ufw allow "$ssh_port/tcp" comment 'Preserve current SSH access'
fi
# Keep HTTP(S) closed until owner enrollment and public-exposure checks pass.
ufw insert 1 deny 8000/tcp comment 'Local application only'
ufw insert 1 deny 80/tcp comment 'Pending exposure approval'
ufw insert 1 deny 443/tcp comment 'Pending exposure approval'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
if ! command -v caddy >/dev/null; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi
systemctl disable --now caddy
printf '%s\n' 'Caddy installed but stopped. Public 80/443 remain denied. Configure owner over SSH before exposure.'
