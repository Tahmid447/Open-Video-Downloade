"""No private keys or application credentials are embedded in instance metadata."""
import hashlib
import ipaddress
import re
import shlex
from pathlib import Path


def render(source_sha, ssh_cidr):
    if not re.fullmatch(r'[0-9a-f]{40}', source_sha):
        raise ValueError('Invalid source revision')
    network=ipaddress.ip_network(ssh_cidr,strict=True)
    if network.version!=4 or network.prefixlen!=32 or not network.network_address.is_global:
        raise ValueError('SSH requires a single public IPv4 source')
    digest=hashlib.sha256(Path(__file__).with_name('bootstrap.sh').read_bytes()).hexdigest()
    # Hash ties the fetched script to the reviewed launcher checkout as well as
    # the immutable source revision. The installer source also uses that SHA.
    return f'''#!/bin/bash
set -euo pipefail
umask 077
install -d -m 0700 /var/lib/ovd-bootstrap
if ! command -v curl >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl ca-certificates
fi
cat > /etc/ovd-bootstrap.conf <<'OVD_CONFIG'
OVD_SOURCE_SHA={shlex.quote(source_sha)}
OVD_BOOTSTRAP_SSH_CIDR={shlex.quote(ssh_cidr)}
OVD_CONFIG
curl --retry 3 --retry-delay 5 --fail --silent --show-error --location --proto '=https' --tlsv1.2 'https://raw.githubusercontent.com/Tahmid447/Open-Video-Downloade/{source_sha}/deploy/oracle/bootstrap.sh' -o /var/lib/ovd-bootstrap/bootstrap.sh
printf '{digest}  /var/lib/ovd-bootstrap/bootstrap.sh\\n' | sha256sum --check --status
chmod 0700 /var/lib/ovd-bootstrap/bootstrap.sh
cat > /var/lib/ovd-bootstrap/report-failure.sh <<'OVD_FAILURE'
#!/bin/bash
if [ "$SERVICE_RESULT" != success ]; then echo OVD_PRIVATE_SETUP_FAILED >/dev/console; fi
OVD_FAILURE
chmod 0700 /var/lib/ovd-bootstrap/report-failure.sh
cat > /etc/systemd/system/ovd-bootstrap.service <<'OVD_UNIT'
[Unit]
Description=Private Open Video Downloader initial installation and reboot verification
After=network-online.target cloud-final.service docker.service open-video-downloader.service
Wants=network-online.target
StartLimitIntervalSec=86400
StartLimitBurst=3
[Service]
Type=oneshot
RemainAfterExit=yes
UMask=0077
ExecStart=/var/lib/ovd-bootstrap/bootstrap.sh
ExecStopPost=/var/lib/ovd-bootstrap/report-failure.sh
TimeoutStartSec=2700
Restart=on-failure
RestartSec=300
StandardOutput=append:/var/log/ovd-bootstrap.log
StandardError=append:/var/log/ovd-bootstrap.log
[Install]
WantedBy=multi-user.target
OVD_UNIT
systemctl daemon-reload
systemctl enable ovd-bootstrap
systemctl start --no-block ovd-bootstrap
'''
