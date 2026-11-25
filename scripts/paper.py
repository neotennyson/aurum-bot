#!/usr/bin/env python3
"""AURUM paper trading runner (replay / live / status). No live orders, ever."""

from __future__ import annotations

import argparse
import json
import sys
import time as time_module
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars, parse_timestamp  # noqa: E402
from aurum.config import load_run_config  # noqa: E402
from aurum.dhan import fetch_dhan_daily  # noqa: E402
from aurum.paper import PaperEngine  # noqa: E402


def summarize(state: dict) -> dict:
    metrics = state.get("metrics", {})
    return {
        "mode": state.get("mode"),
        "status": state.get("status"),
        "symbol": state.get("symbol"),
        "strategy": state.get("strategy"),
        "last_timestamp": state.get("last_timestamp"),
        "equity": state.get("equity"),
        "cash": state.get("cash"),
        "return": metrics.get("total_return"),
        "trades": len(state.get("trades", [])),
        "position": state.get("position"),
        "pending": state.get("pending"),
        "sharpe": metrics.get("sharpe"),
        "max_drawdown": metrics.get("max_drawdown"),
    }


def cmd_replay(args: argparse.Namespace) -> None:
    config = load_run_config(args.config)
    engine = PaperEngine(config, args.state)
    engine.reset()
    bars = load_bars(args.data, config.symbol)
    start = 0
    if args.start:
        cutoff = parse_timestamp(args.start)
        start = next((index for index, bar in enumerate(bars) if bar.timestamp >= cutoff), len(bars) - 1)
    engine.initialize_history(bars[:start]) if start > 0 else engine.initialize_history(bars[:1])
    for bar in bars[start:]:
        engine.process_bar(bar)
    print(json.dumps(summarize(engine.state), indent=2, default=str))


def _today_bar_is_final(now_ist: datetime) -> bool:
    return now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 45)


def cmd_live(args: argparse.Namespace) -> None:
    config = load_run_config(args.config)
    engine = PaperEngine(config, args.state)
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=args.lookback_days)
    while True:
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        allow_today = args.allow_partial or _today_bar_is_final(now_ist)
        last = engine.state.get("last_timestamp")
        if args.segment == "IDX_I":
            bars = fetch_dhan_daily(
                "13", start.isoformat(), end.isoformat(),
                exchange_segment="IDX_I", instrument="INDEX", symbol_alias=config.symbol,
            )
        else:
            bars = fetch_dhan_daily(
                config.symbol, start.isoformat(), end.isoformat(),
                exchange_segment=args.segment, instrument=args.instrument,
            )
        processable = [
            bar for bar in bars
            if (last is None or bar.timestamp.isoformat() > last)
            and (allow_today or bar.timestamp.date() != now_ist.date())
        ]
        for bar in processable:
            engine.process_bar(bar)
            last = bar.timestamp.isoformat()
        if not args.loop:
            break
        time_module.sleep(max(60, args.poll))
    print(json.dumps(summarize(engine.state), indent=2, default=str))


def cmd_status(args: argparse.Namespace) -> None:
    path = Path(args.state)
    if not path.exists():
        print(json.dumps({"status": "no_state", "path": str(path)}))
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(summarize(state), indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-paper")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay")
    replay.add_argument("--data", required=True)
    replay.add_argument("--config", required=True)
    replay.add_argument("--state", default="data/paper_state.json")
    replay.add_argument("--start", help="ISO timestamp to begin paper trading (history before it only warms up indicators)")
    replay.set_defaults(func=cmd_replay)

    live = sub.add_parser("live")
    live.add_argument("--config", required=True)
    live.add_argument("--state", default="data/paper_state.json")
    live.add_argument("--segment", default="IDX_I")
    live.add_argument("--instrument", default="INDEX")
    live.add_argument("--lookback-days", type=int, default=900)
    live.add_argument("--loop", action="store_true")
    live.add_argument("--poll", type=int, default=1800)
    live.add_argument("--allow-partial", action="store_true", help="process an intraday partial candle for today (not recommended)")
    live.set_defaults(func=cmd_live)

    status = sub.add_parser("status")
    status.add_argument("--state", default="data/paper_state.json")
    status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
