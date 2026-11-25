#!/usr/bin/env python3
"""Capital and data-bias study — the honest small-capital answer.

1. NIFTY sleeves in REAL 1-lot sizing (multiplier 75) at ₹2-5L capital.
2. Leveraged buy-and-hold comparison (does it get margin-called?).
3. Stock sleeve with real lot rounding at ₹5-20L.
4. Survivorship: today's F&O list vs names listed before 2016.
5. Time-varying liquidity eligibility (median 60-day turnover floor).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars  # noqa: E402
from aurum.config import CostConfig, RiskConfig, RunConfig  # noqa: E402
from aurum.engine import BacktestEngine  # noqa: E402
from aurum.stocks import StockSleeveConfig, backtest_stock_sleeve, load_series  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGIME = {"sma_period": 200, "exit_buffer_atr": 0.5, "allow_short": False, "stop_atr": 3.5}
TSMOM = {"lookback": 63, "exit_lookback": 21, "regime_sma": 200, "allow_short": False, "stop_atr": 3.0}


def index_config(family: str, capital: float, margin_cap: float) -> RunConfig:
    return RunConfig(
        symbol="48704", timeframe="1d", bar_minutes=375, intraday_only=False,
        strategy=family, params=REGIME if family == "regimeoverlay" else TSMOM,
        costs=CostConfig(),
        risk=RiskConfig(
            capital=capital, risk_per_trade_pct=0.02, sizing_mode="notional",
            target_notional_pct=4.0, max_notional_pct=5.0, margin_pct=0.15,
            max_margin_pct=margin_cap, max_quantity=1, multiplier=75.0,
            daily_loss_pct=0.12, max_trades_per_day=1,
            max_consecutive_losses=10, cooldown_bars=1,
        ),
    )


def index_scenarios(bars, holdout_start: date) -> list[dict]:
    rows = []
    for family in ("regimeoverlay", "tsmom"):
        for capital in (200_000, 300_000, 450_000, 500_000):
            for margin_cap in (0.6, 0.9):
                config = index_config(family, capital, margin_cap)
                result = BacktestEngine(config).run([b for b in bars])
                context = [b for b in bars if b.timestamp.date() >= date(2021, 1, 1)]
                holdout = BacktestEngine(config).run(context, trade_from=__import__("datetime").datetime.combine(holdout_start, __import__("datetime").time.min, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata")))
                leverage = result.trades[0].entry_price * 75 / capital if result.trades else 0.0
                rows.append({
                    "family": family.replace("regimeoverlay", "regime").replace("tsmom", "tsmom"),
                    "capital": capital,
                    "margin_cap": margin_cap,
                    "leverage_at_entry": round(leverage, 2),
                    "full_return": round(result.metrics["total_return"], 4),
                    "full_cagr": round(result.metrics["cagr"], 4),
                    "full_dd": round(result.metrics["max_drawdown"], 4),
                    "full_trades": result.metrics["trades"],
                    "holdout_return": round(holdout.metrics["total_return"], 4),
                    "holdout_dd": round(holdout.metrics["max_drawdown"], 4),
                })
    return rows


def stock_scenarios(series, lot_map, market, holdout_start: date) -> list[dict]:
    rows = []
    for capital in (500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000):
        config = StockSleeveConfig(capital=capital)
        result = backtest_stock_sleeve(series, config, start=holdout_start, market_closes=market, lot_map=lot_map)
        metrics = result["metrics"]
        rows.append({
            "capital": capital,
            "holdout_return": round(metrics["total_return"], 4),
            "holdout_dd": round(metrics["max_drawdown"], 4),
            "sharpe": round(metrics["sharpe"], 2),
            "trades": metrics["trades"],
            "avg_holdings": round(metrics.get("avg_holdings", 0), 2),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-capital-study")
    parser.add_argument("--output", default="reports/capital_study.json")
    args = parser.parse_args()

    bars = load_bars(ROOT / "data/nifty_continuous_daily.csv", "48704")
    market = pd.Series([b.close for b in bars], index=[b.timestamp.date() for b in bars])
    stocks = load_series(ROOT / "data/stocks")
    universe = pd.read_csv(ROOT / "data/reference/fno_universe.csv", dtype=str)
    lot_map = dict(zip(universe["symbol"], universe["lot_units"].astype(int)))
    holdout_start = date(2022, 1, 1)

    print("\n=== 1. NIFTY SLEEVES · REAL 1-LOT SIZING AT SMALL CAPITAL ===")
    index_rows = index_scenarios(bars, holdout_start)
    for row in index_rows:
        print(f"  {row['family']:<7} cap ₹{row['capital']/1e5:.1f}L cap{row['margin_cap']:.1f} "
              f"lev {row['leverage_at_entry']:>4.2f}x | full {row['full_return']:>+7.1%} dd {row['full_dd']:>+6.1%} "
              f"trades {row['full_trades']:>3} | holdout {row['holdout_return']:>+7.1%} dd {row['holdout_dd']:>+6.1%}")

    print("\n=== 2. LEVERAGED BUY & HOLD — DOES IT SURVIVE? ===")
    peak = market.cummax()
    index_dd = float((market / peak - 1).min())
    rows = []
    for capital in (200_000, 300_000, 450_000, 500_000):
        leverage = 75 * float(market.iloc[-1]) / capital
        equity_dd = 1 + leverage * index_dd
        rows.append({
            "capital": capital, "leverage": round(leverage, 2),
            "nifty_max_dd": index_dd,
            "equity_at_worst": round(equity_dd, 4),
            "survives": equity_dd > 0,
        })
        print(f"  ₹{capital/1e5:.1f}L at {leverage:.2f}x: NIFTY worst move {index_dd:+.1%} → equity {equity_dd:+.1%} "
              f"({'survives' if equity_dd > 0 else 'MARGIN CALL / BLOWN UP'})")

    print("\n=== 3. STOCK SLEEVE · REAL LOT ROUNDING ===")
    stock_rows = stock_scenarios(stocks, lot_map, market, holdout_start)
    for row in stock_rows:
        print(f"  ₹{row['capital']/1e5:>4.1f}L: holdout {row['holdout_return']:>+7.1%} dd {row['holdout_dd']:>+6.1%} "
              f"sharpe {row['sharpe']:.2f} trades {row['trades']:>3} avg holdings {row['avg_holdings']:.1f}")

    print("\n=== 4. SURVIVORSHIP — TODAY'S LIST vs NAMES LISTED BY 2016 ===")
    pre2016 = {s: b for s, b in stocks.items() if b[0].timestamp.year <= 2016}
    base = backtest_stock_sleeve(stocks, StockSleeveConfig(), start=holdout_start, market_closes=market)["metrics"]
    sub = backtest_stock_sleeve(pre2016, StockSleeveConfig(), start=holdout_start, market_closes=market)["metrics"]
    print(f"  full {len(stocks)} names: holdout {base['total_return']:>+7.1%} dd {base['max_drawdown']:>+6.1%} sharpe {base['sharpe']:.2f}")
    print(f"  pre-2016 {len(pre2016)} names: holdout {sub['total_return']:>+7.1%} dd {sub['max_drawdown']:>+6.1%} sharpe {sub['sharpe']:.2f}")
    print("  (bias = missing delisted/removed names; the pre-2016 subset is the honest sample)")

    print("\n=== 5. TIME-VARYING LIQUIDITY ELIGIBILITY (60-day median turnover floor) ===")
    for floor in (10.0, 25.0, 50.0):
        metrics = backtest_stock_sleeve(stocks, StockSleeveConfig(), start=holdout_start,
                                        market_closes=market, min_turnover_cr=floor)["metrics"]
        print(f"  floor ₹{floor:.0f}Cr: holdout {metrics['total_return']:>+7.1%} dd {metrics['max_drawdown']:>+6.1%} "
              f"sharpe {metrics['sharpe']:.2f} trades {metrics['trades']}")

    report = {
        "index_1_lot_scenarios": index_rows,
        "leveraged_buy_hold": rows,
        "stock_lot_scenarios": stock_rows,
        "survivorship": {"full_names": len(stocks), "pre2016_names": len(pre2016),
                         "full": base, "pre2016": sub},
        "turnover_floors_note": "eligibility requires 60-day median turnover above the floor at signal time",
        "generated": str(date.today()),
    }
    destination = ROOT / args.output
    destination.write_text(json.dumps(report, indent=2, allow_nan=False, default=str), encoding="utf-8")
    print(f"\nreport -> {destination}")


if __name__ == "__main__":
    main()
