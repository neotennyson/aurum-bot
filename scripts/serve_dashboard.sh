#!/usr/bin/env bash
# Keep the AURUM dashboard + its Cloudflare quick tunnel alive.
# Own port (8551) so it never collides with other local apps.
# Managed by launchd (com.aurum.dashboard) with KeepAlive.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

pkill -f "aurum.webapp" 2>/dev/null || true
pkill -f "cloudflared tunnel --url http://localhost:8551" 2>/dev/null || true
sleep 1

nohup python3 -m aurum.webapp --port 8551 >> logs/dashboard.log 2>&1 &
sleep 2

nohup ./bin/cloudflared tunnel --url http://localhost:8551 >> logs/tunnel.log 2>&1 &
sleep 12

URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' logs/tunnel.log | tail -1)
if [ -n "$URL" ]; then
  echo "$URL" > AURUM_DASHBOARD_URL.txt
  echo "$(date '+%F %T') dashboard up at $URL" >> logs/dashboard.log
fi

wait
