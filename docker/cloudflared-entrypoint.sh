#!/bin/sh
set -eu

TARGET_URL="${CLOUDFLARED_TARGET_URL:-http://bot:8080}"
LOG_PATH="${TUNNEL_LOG_PATH:-/shared/cloudflared.out.log}"

mkdir -p "$(dirname "$LOG_PATH")"
rm -f "$LOG_PATH"

cloudflared tunnel --url "$TARGET_URL" --no-autoupdate 2>&1 | tee "$LOG_PATH"
