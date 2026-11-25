#!/usr/bin/env bash
# AURUM supervisor daemon (paper only).
#   * keeps the dashboard (port 8551) and its tunnel alive
#   * runs the daily paper job on weekdays after 16:20 IST (once per day)
#   * runs the quarterly robustness check on/after the first day of Jan/Apr/Jul/Oct
# Runs under your user session permissions, so keep it started with start_aurum.sh.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs state
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
DAILY_STATE="state/last_daily.txt"
QUARTER_STATE="state/last_quarter.txt"

log() { echo "$(date '+%F %T') $*" >> logs/daemon.log; }

http_ok() { [ "$(curl -s -m 12 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true)" = "200" ]; }

ensure_dashboard() {
  # terminal process
  if pgrep -f "aurum.webapp" >/dev/null 2>&1; then
    if ! http_ok "http://localhost:8551/"; then
      log "terminal unhealthy, restarting"
      pkill -f "aurum.webapp" 2>/dev/null || true
      sleep 2
    fi
  fi
  if ! pgrep -f "aurum.webapp" >/dev/null 2>&1; then
    log "starting terminal"
    nohup "$PYTHON_BIN" -m aurum.webapp --port 8551 >> logs/dashboard.log 2>&1 &
    sleep 2
  fi

  # tunnel process + end-to-end health
  if pgrep -f "cloudflared tunnel --url http://localhost:8551" >/dev/null 2>&1; then
    URL=$(cat AURUM_DASHBOARD_URL.txt 2>/dev/null || true)
    if [ -z "$URL" ] || ! http_ok "$URL"; then
      log "tunnel unhealthy, rotating"
      pkill -f "cloudflared tunnel --url http://localhost:8551" 2>/dev/null || true
      sleep 2
    fi
  fi
  if ! pgrep -f "cloudflared tunnel --url http://localhost:8551" >/dev/null 2>&1; then
    log "starting tunnel"
    nohup ./bin/cloudflared tunnel --url http://localhost:8551 >> logs/tunnel.log 2>&1 &
    sleep 15
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' logs/tunnel.log | tail -1)
    if [ -n "$URL" ]; then
      echo "$URL" > AURUM_DASHBOARD_URL.txt
      log "public url $URL"
    fi
  fi
}

while true; do
  ensure_dashboard

  today=$(date +%F)
  minutes=$((10#$(date +%H) * 60 + 10#$(date +%M)))
  weekday=$(date +%u)
  month=$(date +%m)
  day=$(date +%d)

  last_daily=$(cat "$DAILY_STATE" 2>/dev/null || echo "")
  if [ "$weekday" -le 5 ] && [ "$minutes" -ge 980 ] && [ "$last_daily" != "$today" ]; then
    log "daily job start"
    bash scripts/daily_job.sh
    echo "$today" > "$DAILY_STATE"
    log "daily job done"
  fi

  quarter_label="$(date +%Y)Q$(( (10#$month - 1) / 3 + 1 ))"
  last_quarter=$(cat "$QUARTER_STATE" 2>/dev/null || echo "")
  case "$month" in 01|04|07|10) is_quarter_month=1;; *) is_quarter_month=0;; esac
  if [ "$is_quarter_month" = "1" ] && [ "$last_quarter" != "$quarter_label" ]; then
    log "quarterly check start ($quarter_label)"
    "$PYTHON_BIN" scripts/quarterly_check.py >> logs/quarterly.log 2>&1
    echo "$quarter_label" > "$QUARTER_STATE"
    log "quarterly check done ($quarter_label)"
  fi

  sleep 300
done
