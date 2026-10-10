#!/bin/bash
set -euo pipefail
RELEASE="$(cd "$(dirname "$0")/.." && pwd -P)"
compose=(docker compose --project-name open-video-downloader --env-file "$RELEASE/deploy/.env" --env-file "$RELEASE/deploy/image.env" -f "$RELEASE/deploy/docker-compose.yml")
id="$("${compose[@]}" ps -q downloader)"
[ -n "$id" ] || { echo 'No downloader container' >&2; exit 1; }
docker inspect "$id" | python3 -c '
import json,sys
c=json.load(sys.stdin)[0]
assert c["State"]["Health"]["Status"] == "healthy", "Container is unhealthy"
assert c["HostConfig"]["PortBindings"]["8000/tcp"] == [{"HostIp":"127.0.0.1", "HostPort":"8000"}], "Unsafe port publication"
assert c["Config"]["User"] == "10001:10001", "Unexpected user"
assert c["HostConfig"]["ReadonlyRootfs"], "Writable root filesystem"
assert c["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
'
curl -fsS --max-time 5 http://127.0.0.1:8000/api/health | python3 -c 'import json,sys; assert json.load(sys.stdin)["ok"]'
iptables -w -C FORWARD -j DOCKER-USER
iptables -w -C DOCKER-USER -j OVD-EGRESS
iptables -w -C INPUT -j OVD-HOST
# Fail if any public Funnel mapping exists, without enabling/changing it.
tailscale serve status --json | python3 -c '
import json,sys
config=json.load(sys.stdin)
def safe(obj):
    if isinstance(obj,dict):
        for k,v in obj.items():
            if k == "AllowFunnel" and isinstance(v,dict) and any(v.values()): raise SystemExit("Public sharing is configured; stop and inspect Tailscale settings.")
            safe(v)
    elif isinstance(obj,list):
        for v in obj: safe(v)
safe(config)
'
echo 'Container healthy; host port is localhost only; private-network egress guards installed.'

tailscale status --json | python3 -c 'import json,sys; assert json.load(sys.stdin).get("BackendState") == "Running", "Tailscale is not connected"'
private_name="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
[[ "$private_name" =~ ^[a-zA-Z0-9.-]+\.ts\.net$ ]] || { echo 'Unexpected Tailscale DNS name' >&2; exit 1; }
curl -fsS --retry 5 --retry-delay 2 --retry-all-errors --max-time 10 "https://$private_name/api/health" | python3 -c 'import json,sys; assert json.load(sys.stdin)["ok"]'
echo "Private HTTPS health check passed: https://$private_name/"
