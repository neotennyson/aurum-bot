#!/usr/bin/env python3
"""Stock-F&O momentum sleeve research: full period, holdout window, cost stress."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars  # noqa: E402
from aurum.engine import performance_metrics  # noqa: E402
from aurum.models import EquityPoint  # noqa: E402
from aurum.stocks import (  # noqa: E402
    StockSleeveConfig,
    backtest_stock_sleeve,
    frames,
    load_series,
    save_report,
)


def nifty_closes(path: str | Path, symbol: str, start: date, end: date) -> pd.Series:
    bars = [bar for bar in load_bars(path, symbol) if start <= bar.timestamp.date() <= end]
    return pd.Series([bar.close for bar in bars], index=[bar.timestamp.date() for bar in bars])


def nifty_curve(path: str | Path, symbol: str, start: date, end: date, capital: float) -> dict:
    bars = [bar for bar in load_bars(path, symbol) if start <= bar.timestamp.date() <= end]
    curve = []
    peak = capital
    first = bars[0].close if bars else 0.0
    for bar in bars:
        equity = capital * bar.close / first if first else capital
        peak = max(peak, equity)
        curve.append(EquityPoint(bar.timestamp, equity, equity / peak - 1.0, bar.close, 0))
    return {"metrics": performance_metrics(curve, [], capital, 375), "curve": [p.to_dict() for p in curve]}


def equal_weight_curve(series, start: date, end: date, capital: float) -> dict:
    opens, closes = frames(series)
    closes = closes[(closes.index >= start) & (closes.index <= end)]
    normalised = closes.ffill()
    normalised = normalised / normalised.iloc[0]
    average = normalised.mean(axis=1)
    curve = []
    peak = capital
    for day, value in average.items():
        equity = capital * float(value)
        peak = max(peak, equity)
        curve.append(EquityPoint(pd.Timestamp(day).to_pydatetime(), equity, equity / peak - 1.0, 0.0, 0))
    return {"metrics": performance_metrics(curve, [], capital, 375), "curve": [p.to_dict() for p in curve]}


def print_metrics(label: str, metrics: dict) -> None:
    print(
        f"{label:<26} ret {metrics['total_return']:>8.1%} cagr {metrics['cagr']:>6.1%} "
        f"dd {metrics['max_drawdown']:>7.1%} sharpe {metrics['sharpe']:>5.2f} "
        f"trades {metrics['trades']:>4} win {metrics['win_rate']:>4.0%} pf {metrics['profit_factor'] or 0:>4.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-stocks-research")
    parser.add_argument("--data-dir", default="data/stocks")
    parser.add_argument("--nifty", default="data/nifty_continuous_daily.csv")
    parser.add_argument("--nifty-symbol", default="48704")
    parser.add_argument("--output", default="reports/stocks_research.json")
    parser.add_argument("--holdout-start", default="2022-01-01")
    args = parser.parse_args()

    series = load_series(args.data_dir)
    print(f"universe: {len(series)} stock futures with usable history")
    config = StockSleeveConfig()
    market = nifty_closes(args.nifty, args.nifty_symbol, date(2016, 1, 1), date.today())
    liquidity_floor = 10.0  # ₹ crore, 60-day median turnover at signal time (point-in-time eligibility)

    full = backtest_stock_sleeve(series, config, market_closes=market, min_turnover_cr=liquidity_floor)
    holdout_start = date.fromisoformat(args.holdout_start)
    holdout = backtest_stock_sleeve(series, config, start=holdout_start, market_closes=market, min_turnover_cr=liquidity_floor)
    holdout_no_gate = backtest_stock_sleeve(series, config, start=holdout_start, min_turnover_cr=liquidity_floor)
    full_no_gate = backtest_stock_sleeve(series, config, min_turnover_cr=liquidity_floor)
    stress_config = StockSleeveConfig(slippage_bps=8.0, roll_cost_bps_monthly=6.0)
    holdout_stress = backtest_stock_sleeve(series, stress_config, start=holdout_start, market_closes=market, min_turnover_cr=liquidity_floor)
    bench = nifty_curve(args.nifty, args.nifty_symbol, holdout_start, date.today(), config.capital)
    equal_weight = equal_weight_curve(series, holdout_start, date.today(), config.capital)

    print("\n--- stock sleeve (continuous stock futures, whole shares, F&O costs, NIFTY 200-SMA gate) ---")
    print_metrics("full period", full["metrics"])
    print_metrics(f"holdout {holdout_start}+", holdout["metrics"])
    print_metrics("holdout no gate", holdout_no_gate["metrics"])
    print_metrics("holdout cost stress", holdout_stress["metrics"])
    print_metrics("NIFTY same window", bench["metrics"])
    print_metrics("equal-weight universe", equal_weight["metrics"])

    report = {
        "method": "cross_sectional_momentum_fixed_parameters",
        "note": (
            "Fixed rules: weekly rebalance, eligible = close > 200-SMA and 126-day return > 0, "
            "top 8 by momentum, keep while inside rank 16, equal notional, whole shares, "
            "4 bps slippage and 3 bps/month roll cost. Market gate: the book only invests while "
            "NIFTY itself is above its 200-day average, otherwise it holds cash. Parameters were "
            "fixed before running; the gate was added after crisis-window analysis and is reported "
            "both with and without for transparency."
        ),
        "config": config.to_dict(),
        "universe_size": len(series),
        "universe": sorted(series),
        "full": {key: full[key] for key in ("metrics", "start", "end", "turnover")},
        "full_curve": full["equity_curve"],
        "holdout_start": str(holdout_start),
        "holdout": {key: holdout[key] for key in ("metrics", "turnover")},
        "holdout_no_gate": holdout_no_gate["metrics"],
        "full_no_gate": full_no_gate["metrics"],
        "market_gate": "NIFTY above its 200-day SMA",
        "holdout_curve": holdout["equity_curve"],
        "holdout_trades": holdout["trades"],
        "holdout_stress": {"metrics": holdout_stress["metrics"]},
        "nifty_benchmark": bench["metrics"],
        "nifty_curve": bench["curve"],
        "equal_weight_benchmark": equal_weight["metrics"],
        "disclaimer": "Paper/research only. Past performance does not guarantee future profits.",
    }
    save_report(report, args.output)
    print(f"\nreport -> {args.output}")


if __name__ == "__main__":
    main()
