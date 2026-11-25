#!/usr/bin/env python3
"""Reset AURUM paper books to FORWARD-ONLY mode.

The previous books were replays of historical CSVs and are archived under
data/archive/. Fresh books start at the latest market date with the full
capital intact and zero trades: every trade from here on is genuine
out-of-sample paper trading recorded in real time.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from aurum.bars import load_bars  # noqa: E402
from aurum.config import load_run_config  # noqa: E402
from aurum.paper import PaperEngine  # noqa: E402
from aurum.stocks import StockPaperEngine, StockSleeveConfig, frames, load_series  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data/archive"


def main() -> None:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    today = date.today()

    for name, state_path in (
        ("regime_daily", ROOT / "data/paper_regime.json"),
        ("tsmom_daily", ROOT / "data/paper_tsmom.json"),
    ):
        if state_path.exists():
            shutil.move(str(state_path), str(ARCHIVE / state_path.name))
        config = load_run_config(ROOT / f"config/{name}.json")
        bars = load_bars(ROOT / "data/nifty_continuous_daily.csv", config.symbol)
        engine = PaperEngine(config, state_path)
        engine.reset()
        engine.initialize_history(bars)  # warm indicators only, no trades
        print(f"{state_path.name:<22} forward from {engine.state['last_timestamp'][:10]} · {engine.state['status']} · trades {len(engine.state['trades'])}")

    stock_path = ROOT / "data/paper_stocks.json"
    if stock_path.exists():
        shutil.move(str(stock_path), str(ARCHIVE / stock_path.name))
    series = load_series(ROOT / "data/stocks")
    _, closes = frames(series)
    latest = closes.index[-1]
    stock_engine = StockPaperEngine(StockSleeveConfig(), stock_path)
    stock_engine.reset()
    stock_engine.state["last_date"] = latest.isoformat()
    stock_engine.state["status"] = "waiting_for_data"
    stock_engine._persist()
    print(f"{stock_path.name:<22} forward from {latest.isoformat()} · waiting_for_data · trades 0")

    marker = {
        "forward_start": today.isoformat(),
        "note": "Forward-only paper trading. Historical replay results live in data/archive and in reports/.",
    }
    (ROOT / "data/forward_start.json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
    print("marker -> data/forward_start.json")


if __name__ == "__main__":
    main()
