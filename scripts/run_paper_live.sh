#!/usr/bin/env bash
# Advance AURUM's paper books with fresh data from Dhan (read-only).
# Funded book: regime sleeve + stock sleeve. tsmom keeps updating as observation only.
# Run after market close (after ~15:45 IST).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/paper.py live --config config/regime_daily.json --state data/paper_regime.json "$@"
python3 scripts/stocks_paper.py live --state data/paper_stocks.json "$@"
python3 scripts/paper.py live --config config/tsmom_daily.json --state data/paper_tsmom.json "$@"
