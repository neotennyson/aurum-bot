#!/usr/bin/env python3
"""Compounding account study: ₹4.5L start, real NIFTY lot sizing, margin-scaled.

Margin-driven sizing means the account starts with one lot and adds a lot
whenever equity covers the margin for the next one - genuine compounding.
Research/replay only; the forward paper book trades this config live.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars  # noqa: E402
from aurum.config import load_run_config  # noqa: E402
from aurum.engine import BacktestEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-compound-study")
    parser.add_argument("--output", default="reports/compound_study.json")
    args = parser.parse_args()

    config = load_run_config(ROOT / "config/compound_daily.json")
    bars = load_bars(ROOT / "data/nifty_continuous_daily.csv", config.symbol)

    full = BacktestEngine(config).run(bars)
    context = [b for b in bars if b.timestamp.date() >= date(2021, 1, 1)]
    holdout = BacktestEngine(config).run(context, trade_from=datetime.combine(date(2022, 1, 1), time.min, tzinfo=IST))

    max_qty = max((point.position for point in full.equity_curve), default=0)
    final_price = bars[-1].close
    lots = full.equity_curve[-1].position
    margin_per_lot = final_price * 75 * config.risk.margin_pct

    report = {
        "config": config.to_dict(),
        "full": full.metrics,
        "holdout_2022_plus": holdout.metrics,
        "max_lots_used": max_qty,
        "final_lots": lots,
        "final_equity": round(full.final_equity),
        "margin_per_lot_today": round(margin_per_lot),
        "note": (
            "Replay research only - not a forward result. Margin-driven sizing: one lot while "
            "equity covers its margin, more lots automatically as the account compounds."
        ),
    }
    destination = ROOT / args.output
    destination.write_text(json.dumps(report, indent=2, allow_nan=False, default=str), encoding="utf-8")

    print(f"compound study (replay, {bars[0].timestamp.date()} -> {bars[-1].timestamp.date()})")
    print(f"  full period:   return {full.metrics['total_return']:>+8.1%}  cagr {full.metrics['cagr']:>+6.1%}  "
          f"dd {full.metrics['max_drawdown']:>+6.1%}  trades {full.metrics['trades']}  max lots {max_qty}  final ₹{full.final_equity:,.0f}")
    print(f"  holdout 2022+: return {holdout.metrics['total_return']:>+8.1%}  dd {holdout.metrics['max_drawdown']:>+6.1%}  trades {holdout.metrics['trades']}")
    print(f"  margin per lot today: ₹{margin_per_lot:,.0f} (next lot needs ≈ ₹{margin_per_lot / config.risk.max_margin_pct * (lots + 1):,.0f} equity)")
    print(f"\nreport -> {destination}")


if __name__ == "__main__":
    main()
