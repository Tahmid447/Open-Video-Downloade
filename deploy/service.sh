#!/bin/bash
set -euo pipefail
RELEASE="$(cd "$(dirname "$0")/.." && pwd -P)"
compose=(docker compose --project-name open-video-downloader --env-file "$RELEASE/deploy/.env" --env-file "$RELEASE/deploy/image.env" -f "$RELEASE/deploy/docker-compose.yml")
case "${1:-}" in
  start) "$RELEASE/deploy/prepare-cloud-storage.sh"; "${compose[@]}" up -d --no-build --wait --wait-timeout 90 ;;
  stop) "${compose[@]}" stop ;;
  *) echo "Usage: $0 start|stop" >&2; exit 2 ;;
esac
