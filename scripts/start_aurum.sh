#!/usr/bin/env bash
# Start (or restart) the AURUM supervisor daemon after a reboot.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs state
pkill -f "aurum_daemon.sh" 2>/dev/null || true
sleep 1
nohup bash scripts/aurum_daemon.sh >/dev/null 2>&1 &
sleep 3
if pgrep -f "aurum_daemon.sh" >/dev/null; then
  echo "AURUM daemon started (pid $(pgrep -f aurum_daemon.sh | head -1))"
  echo "dashboard: $(cat AURUM_DASHBOARD_URL.txt 2>/dev/null || echo 'starting - check logs/daemon.log')"
else
  echo "failed to start; check logs/daemon.log"
fi
