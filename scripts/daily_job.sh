#!/usr/bin/env bash
# AURUM daily paper job. Runs after market close (scheduled 16:20 IST weekdays).
# Advances funded sleeves (regime + stocks) and the tsmom observation sleeve.
# Read-only broker access; no order path exists anywhere in AURUM.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/daily_$(date +%F).log"

{
  echo "=== AURUM daily job $(date '+%F %T %Z') ==="

  echo "--- regime (funded) ---"
  python3 scripts/paper.py live --config config/regime_daily.json --state data/paper_regime.json 2>&1 | tail -6
  REGIME_RC=$?

  echo "--- compound (funded, 1-lot) ---"
  python3 scripts/paper.py live --config config/compound_daily.json --state data/paper_compound.json 2>&1 | tail -6
  COMPOUND_RC=$?

  echo "--- stocks (funded) ---"
  python3 scripts/stocks_paper.py live --state data/paper_stocks.json 2>&1 | tail -6
  STOCKS_RC=$?

  echo "--- tsmom (observation) ---"
  python3 scripts/paper.py live --config config/tsmom_daily.json --state data/paper_tsmom.json 2>&1 | tail -6
  TSMOM_RC=$?

  python3 - "$REGIME_RC" "$COMPOUND_RC" "$STOCKS_RC" "$TSMOM_RC" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path

status = {"as_of": datetime.now().isoformat(timespec="seconds"), "sleeves": {}}
for name, path in (("regime", "data/paper_regime.json"),
                   ("compound", "data/paper_compound.json"),
                   ("stocks", "data/paper_stocks.json"),
                   ("tsmom", "data/paper_tsmom.json")):
    try:
        state = json.loads(Path(path).read_text())
        status["sleeves"][name] = {
            "equity": state.get("equity"),
            "status": state.get("status"),
            "last": state.get("last_date") or state.get("last_timestamp"),
        }
    except Exception as error:  # noqa: BLE001
        status["sleeves"][name] = {"error": str(error)}
status["exit_codes"] = {"regime": sys.argv[1], "compound": sys.argv[2], "stocks": sys.argv[3], "tsmom": sys.argv[4]}
Path("data/last_run.json").write_text(json.dumps(status, indent=2))
print("status ->", json.dumps(status["sleeves"]))
PY
  echo "=== done $(date '+%F %T') ==="
} >> "$LOG" 2>&1
