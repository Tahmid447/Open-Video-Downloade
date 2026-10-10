#!/bin/bash
# Dedicated bridge rules: do not flush or alter unrelated firewall chains.
set -euo pipefail
if [ "${1:-}" != pre ]; then
  iptables -w -C FORWARD -j DOCKER-USER 2>/dev/null || { echo 'Docker must use its supported iptables firewall backend; DOCKER-USER is not attached.' >&2; exit 2; }
fi
iptables -w -N DOCKER-USER 2>/dev/null || true
iptables -w -N OVD-EGRESS 2>/dev/null || true
iptables -w -F OVD-EGRESS
iptables -w -A OVD-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# Prevent direct routing into this container from LAN/public/tailnet interfaces.
iptables -w -A OVD-EGRESS -o ovd0 -j REJECT
for range in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
  iptables -w -A OVD-EGRESS -i ovd0 -d "$range" -j REJECT
done
iptables -w -A OVD-EGRESS -i ovd0 -p tcp -m multiport --dports 80,443,53 -j RETURN
iptables -w -A OVD-EGRESS -i ovd0 -p udp --dport 53 -j RETURN
iptables -w -A OVD-EGRESS -i ovd0 -j REJECT
iptables -w -A OVD-EGRESS -j RETURN
iptables -w -C DOCKER-USER -j OVD-EGRESS 2>/dev/null || iptables -w -I DOCKER-USER 1 -j OVD-EGRESS
# Container-to-host requests do not pass through DOCKER-USER.
iptables -w -N OVD-HOST 2>/dev/null || true
iptables -w -F OVD-HOST
iptables -w -A OVD-HOST -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -w -A OVD-HOST -i ovd0 -j REJECT
iptables -w -A OVD-HOST -j RETURN
iptables -w -C INPUT -j OVD-HOST 2>/dev/null || iptables -w -I INPUT 1 -j OVD-HOST
