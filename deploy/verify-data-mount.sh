#!/bin/bash
# Also runs before Docker starts: no Docker calls or filesystem writes here.
set -euo pipefail
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = aarch64 ] && [ "$(id -u)" -eq 0 ] || { echo 'Requires sudo on the Ubuntu ARM64 server.' >&2; exit 2; }
mountpoint -q /srv/ovd-data || { echo 'Mount the approved block volume at /srv/ovd-data first. No boot-disk fallback.' >&2; exit 2; }
expected=/etc/open-video-downloader/data-volume.uuid
[ -s "$expected" ] || { echo 'Record the approved block filesystem UUID before installation.' >&2; exit 2; }
actual="$(findmnt -rn -o UUID -T /srv/ovd-data)"
[ "$actual" = "$(cat "$expected")" ] && [ -n "$actual" ] || { echo 'Data-volume UUID mismatch. Refusing deployment.' >&2; exit 2; }
