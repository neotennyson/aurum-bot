#!/usr/bin/env python3
"""Paper trading for the stock-F&O momentum sleeve (replay / live / status)."""

from __future__ import annotations

import argparse
import json
import sys
import time as time_module
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars, save_bars  # noqa: E402
from aurum.dhan import fetch_dhan_daily  # noqa: E402
from aurum.stocks import StockPaperEngine, StockSleeveConfig, frames, load_series  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data/reference/fno_universe.csv"
STOCK_DIR = ROOT / "data/stocks"


def universe_ids() -> dict[str, str]:
    if not UNIVERSE.exists():
        raise FileNotFoundError("run scripts/fetch_stocks.py first to build data/reference/fno_universe.csv")
    frame = pd.read_csv(UNIVERSE, dtype=str)
    return dict(zip(frame["symbol"], frame["security_id"]))


def market_closed() -> bool:
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    return now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 45)


def merge_bars(existing: list, fresh: list) -> list:
    """Merge by timestamp, fresh wins; drop today's partial candle before close."""
    by_timestamp = {bar.timestamp: bar for bar in existing}
    for bar in fresh:
        if bar.timestamp.date() == date.today() and not market_closed():
            continue
        by_timestamp[bar.timestamp] = bar
    return sorted(by_timestamp.values(), key=lambda bar: bar.timestamp)


def refresh_market() -> None:
    """Keep the NIFTY gate history current (same read-only Dhan endpoint)."""
    destination = ROOT / "data/nifty_continuous_daily.csv"
    existing = []
    start = "2016-01-01"
    if destination.exists():
        try:
            existing = load_bars(destination, "48704")
            start = existing[-1].timestamp.date().isoformat()  # re-fetch last day too, fresh wins
        except ValueError:
            existing = []
    if start > date.today().isoformat():
        return
    try:
        fresh = fetch_dhan_daily("13", start, date.today().isoformat(), exchange_segment="IDX_I",
                                 instrument="INDEX", symbol_alias="48704")
    except Exception as error:  # noqa: BLE001
        print(f"market refresh failed: {type(error).__name__}: {error}")
        return
    merged = merge_bars(existing, fresh)
    if merged:
        save_bars(destination, merged)


def refresh_symbols(symbols: list[str], sleep: float = 1.1) -> int:
    ids = universe_ids()
    today = date.today()
    updated = 0
    for symbol in symbols:
        destination = STOCK_DIR / f"{symbol}.csv"
        start = "2016-01-01"
        existing = []
        if destination.exists():
            try:
                existing = load_bars(destination, None)
                start = existing[-1].timestamp.date().isoformat()  # re-fetch last day too, fresh wins
            except ValueError:
                existing = []
        if start > today.isoformat():
            continue
        try:
            fresh = fetch_dhan_daily(ids[symbol], start, today.isoformat(), exchange_segment="NSE_FNO", instrument="FUTSTK")
        except Exception as error:  # noqa: BLE001
            print(f"FAIL {symbol}: {type(error).__name__}: {error}")
            continue
        merged = merge_bars(existing, fresh)
        if merged:
            save_bars(destination, merged)
            updated += 1
        time_module.sleep(sleep)
    return updated


def signal_rows(sma: pd.DataFrame, momentum: pd.DataFrame, closes: pd.DataFrame, day: date):
    history = [d for d in closes.index if d < day]
    if not history:
        return None, {}, {}, {}
    signal_day = history[-1]
    close_row = {s: float(v) for s, v in closes.loc[signal_day].items() if not pd.isna(v)}
    sma_row = {s: float(v) for s, v in sma.loc[signal_day].items() if not pd.isna(v)}
    mom_row = {s: float(v) for s, v in momentum.loc[signal_day].items() if not pd.isna(v)}
    return signal_day, close_row, sma_row, mom_row


def load_market(path: str | Path = "data/nifty_continuous_daily.csv", symbol: str = "48704") -> pd.Series:
    try:
        bars = load_bars(path, symbol)
    except (FileNotFoundError, ValueError):
        print("warning: NIFTY file not found, market gate disabled")
        return pd.Series(dtype=float)
    return pd.Series([bar.close for bar in bars], index=[bar.timestamp.date() for bar in bars])


def gate_ok(market: pd.Series, signal_day: date | None) -> bool:
    if market.empty or signal_day is None:
        return True
    history = market.loc[:signal_day]
    if len(history) < 200:
        return True
    return float(history.iloc[-1]) > float(history.iloc[-200:].mean())


def run(engine: StockPaperEngine, open_frame: pd.DataFrame, close_frame: pd.DataFrame,
        sma: pd.DataFrame, momentum: pd.DataFrame, start: date, end: date, market: pd.Series) -> None:
    days = [day for day in close_frame.index if start <= day <= end]
    for day in days:
        signal_day, close_row, sma_row, mom_row = signal_rows(sma, momentum, close_frame, day)
        opens = {s: float(v) for s, v in open_frame.loc[day].items() if not pd.isna(v)}
        closes = {s: float(v) for s, v in close_frame.loc[day].items() if not pd.isna(v)}
        engine.advance(day, opens, closes, signal_day, close_row, sma_row, mom_row,
                       market_ok=gate_ok(market, signal_day))


def summarize(engine: StockPaperEngine) -> dict:
    state = engine.state
    metrics = state.get("metrics") or {}
    positions = state.get("positions", {})
    return {
        "mode": state.get("mode"),
        "status": state.get("status"),
        "last_date": state.get("last_date"),
        "equity": state.get("equity"),
        "cash": state.get("cash"),
        "return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "holdings": len(positions),
        "top_holdings": [
            {"symbol": symbol, "quantity": int(entry["quantity"]), "entry": round(entry["entry_price"], 1)}
            for symbol, entry in list(positions.items())[:10]
        ],
        "trades": len(state.get("trades", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-stocks-paper")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay")
    replay.add_argument("--state", default="data/paper_stocks.json")
    replay.add_argument("--start", default="2024-01-01")
    replay.add_argument("--capital", type=float, default=1_000_000.0)
    replay.add_argument("--exposure", type=float, default=1.0)
    replay.set_defaults(mode="replay")

    live = sub.add_parser("live")
    live.add_argument("--state", default="data/paper_stocks.json")
    live.add_argument("--capital", type=float, default=1_000_000.0)
    live.add_argument("--exposure", type=float, default=1.0)
    live.add_argument("--limit", type=int, default=0, help="max symbols to refresh (0 = all)")
    live.add_argument("--sleep", type=float, default=1.1)
    live.add_argument("--allow-partial", action="store_true")
    live.set_defaults(mode="live")

    status = sub.add_parser("status")
    status.add_argument("--state", default="data/paper_stocks.json")
    status.set_defaults(mode="status")

    args = parser.parse_args()
    config = StockSleeveConfig(capital=getattr(args, "capital", 1_000_000.0), exposure=getattr(args, "exposure", 1.0))

    if args.mode == "status":
        path = Path(args.state)
        if not path.exists():
            print(json.dumps({"status": "no_state"}))
            return
        print(json.dumps(summarize(StockPaperEngine(config, path)), indent=2, default=str))
        return

    if args.mode == "live":
        symbols = list(universe_ids())
        if args.limit:
            symbols = symbols[: args.limit]
        updated = refresh_symbols(symbols, args.sleep)
        print(f"refreshed {updated} symbols", flush=True)
        refresh_market()
        engine = StockPaperEngine(config, args.state)
        series = load_series(STOCK_DIR)
        opens, closes = frames(series)
        sma = closes.rolling(config.regime_sma, min_periods=config.regime_sma).mean()
        momentum = closes.pct_change(config.lookback, fill_method=None)
        last = engine.state.get("last_date")
        start = date.fromisoformat(last) + timedelta(days=1) if last else min(closes.index)
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        today_final = now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 45)
        end = date.today() if (today_final or args.allow_partial) else date.today() - timedelta(days=1)
        run(engine, opens, closes, sma, momentum, start, end, load_market())
        print(json.dumps(summarize(engine), indent=2, default=str))
        return

    start = date.fromisoformat(args.start)
    engine = StockPaperEngine(config, args.state)
    engine.reset()
    series = load_series(STOCK_DIR)
    opens, closes = frames(series)
    sma = closes.rolling(config.regime_sma, min_periods=config.regime_sma).mean()
    momentum = closes.pct_change(config.lookback, fill_method=None)
    run(engine, opens, closes, sma, momentum, start, date.today(), load_market())
    print(json.dumps(summarize(engine), indent=2, default=str))


if __name__ == "__main__":
    main()
