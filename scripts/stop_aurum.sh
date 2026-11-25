#!/usr/bin/env bash
# Stop the AURUM daemon and its dashboard/tunnel.
set -uo pipefail
pkill -f "aurum_daemon.sh" 2>/dev/null && echo "daemon stopped" || echo "daemon not running"
pkill -f "aurum/dashboard.py" 2>/dev/null && echo "dashboard stopped" || true
pkill -f "cloudflared tunnel --url http://localhost:8551" 2>/dev/null && echo "tunnel stopped" || true
