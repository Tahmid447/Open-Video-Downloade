#!/bin/bash
# Run on VPS while the public proxy is stopped.
set -euo pipefail
RELEASE="$(cd "$(dirname "$0")/.." && pwd -P)"
"$RELEASE/deploy/prepare-cloud-storage.sh"
compose=(docker compose --project-name open-video-downloader --env-file "$RELEASE/deploy/.env" --env-file "$RELEASE/deploy/image.env" -f "$RELEASE/deploy/docker-compose.yml")
id="$("${compose[@]}" ps -q downloader)"
docker inspect "$id" | python3 -c '
import json,sys
x=json.load(sys.stdin)[0]; h=x["HostConfig"]; e=dict(v.split("=",1) for v in x["Config"]["Env"] if "=" in v)
assert x["Config"]["User"]=="10001:10001" and h["ReadonlyRootfs"] and "ALL" in h["CapDrop"]
assert h["PortBindings"]=={"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"8000"}]}
for key,value in {"COOKIE_SECURE":"1","OVD_REQUIRE_MFA":"1","OVD_REQUIRE_HTTPS":"1","OVD_LOCAL_SETUP":"0","OVD_ALLOW_UNAUTHENTICATED_LOCAL":"0","FORWARDED_ALLOW_IPS":"172.30.80.1","OVD_LOCAL_BROWSER":"0","OVD_REQUIRE_ENCRYPTION":"1","OVD_CREDENTIAL_KEY_FILE":"/run/ovd-keys/master.key"}.items():assert e.get(key)==value,key
assert all(any(m["Destination"]=="/data/"+root for m in x["Mounts"]) for root in ["library","state","config","downloads"])
assert any(m["Destination"]=="/run/ovd-keys" and not m["RW"] for m in x["Mounts"])
print("Private container configuration verified.")
'
"${compose[@]}" exec -T downloader python -c 'import vault; vault.read_key(); print("Separate encryption key is usable; value was not displayed.")'
curl -fsS http://127.0.0.1:8000/api/health >/dev/null
[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/library)" = 401 ]
[ "$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{"password":"invalid-test-password"}' http://127.0.0.1:8000/api/auth/login)" = 403 ]
# Actual Linux socket connections, rather than only input validation.
"${compose[@]}" exec -T downloader python -c '
import socket
for ip in ["169.254.169.254","10.0.0.1","100.64.0.1"]:
    try:socket.create_connection((ip,80),2).close()
    except OSError:continue
    raise SystemExit("Unsafe private egress: "+ip)
print("Private/metadata/tailnet egress blocked.")
'
iptables -w -C DOCKER-USER -j OVD-EGRESS
iptables -w -C INPUT -j OVD-HOST
systemctl is-active --quiet caddy && { echo 'Stop the public proxy during private preparation.' >&2; exit 2; }
echo 'Private preparation checks passed. MFA enrollment and exposure approval are still required.'
