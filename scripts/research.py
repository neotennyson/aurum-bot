#!/usr/bin/env python3
"""Rolling walk-forward research across AURUM strategy families."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars  # noqa: E402
from aurum.config import CostConfig, RiskConfig, RunConfig  # noqa: E402
from aurum.walkforward import default_grid, run_walk_forward  # noqa: E402


def daily_base(symbol: str, family: str, capital: float, risk_pct: float) -> RunConfig:
    directional = family in {"regimeoverlay", "tsmom"}
    return RunConfig(
        symbol=symbol,
        timeframe="1d",
        bar_minutes=375,
        intraday_only=False,
        strategy=family,
        params={},
        costs=CostConfig(),
        risk=RiskConfig(
            capital=capital,
            risk_per_trade_pct=risk_pct,
            sizing_mode="notional" if directional else "risk",
            target_notional_pct=1.0,
            max_notional_pct=1.5,
            margin_pct=0.15,
            max_margin_pct=0.5,
            max_quantity=25,
            multiplier=1.0,
            daily_loss_pct=0.08 if directional else 0.05,
            max_trades_per_day=1,
            max_consecutive_losses=6,
            cooldown_bars=1,
        ),
    )


def intraday_base(symbol: str, family: str, capital: float, risk_pct: float, bar_minutes: int) -> RunConfig:
    base = daily_base(symbol, family, capital, risk_pct)
    base.timeframe = f"{bar_minutes}m"
    base.bar_minutes = bar_minutes
    base.intraday_only = True
    base.session_start = "09:15"
    base.flatten_at = "15:10"
    base.entry_cutoff = "14:30"
    return base


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-research")
    parser.add_argument("--data", required=True)
    parser.add_argument("--symbol", default="48704")
    parser.add_argument("--families", default="regimeoverlay,donchian,pullback,tsmom")
    parser.add_argument("--train", type=int, default=756)
    parser.add_argument("--test", type=int, default=252)
    parser.add_argument("--step", type=int, default=0)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--risk-pct", type=float, default=0.01)
    parser.add_argument("--bar-minutes", type=int, default=375)
    parser.add_argument("--intraday", action="store_true")
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args()

    bars = load_bars(args.data, args.symbol)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"method": "rolling_walk_forward", "data": args.data, "symbol": args.symbol, "families": {}}
    header = f"{'family':<10} {'OOS ret':>9} {'CAGR':>8} {'Sharpe':>7} {'MaxDD':>8} {'trades':>7} {'PF':>6} {'win%':>6} {'+ve win':>8} {'stress':>8} {'bench':>8}"
    print(header)
    print("-" * len(header))
    for family in [item.strip() for item in args.families.split(",") if item.strip()]:
        base = (
            intraday_base(args.symbol, family, args.capital, args.risk_pct, args.bar_minutes)
            if args.intraday
            else daily_base(args.symbol, family, args.capital, args.risk_pct)
        )
        grid = default_grid(family)
        try:
            report = run_walk_forward(
                bars,
                base,
                grid,
                train_bars=args.train,
                test_bars=args.test,
                step_bars=args.step or args.test,
            )
        except ValueError as error:
            print(f"{family:<10} skipped: {error}")
            continue
        destination = output_dir / f"walkforward_{family}.json"
        destination.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        metrics = report["oos_metrics"]
        stress = report["oos_stress_metrics"]
        benchmark = report["benchmark_metrics"]
        summary["families"][family] = {
            "report": str(destination),
            "oos_metrics": metrics,
            "stress_metrics": stress,
            "benchmark_metrics": benchmark,
            "window_count": report["window_count"],
            "positive_windows": report["positive_windows"],
            "selection_counts": report["selection_counts"],
        }
        print(
            f"{family:<10} {metrics.get('total_return', 0):>8.1%} {metrics.get('cagr', 0):>7.1%} "
            f"{metrics.get('sharpe', 0):>7.2f} {metrics.get('max_drawdown', 0):>7.1%} "
            f"{metrics.get('trades', 0):>7d} {metrics.get('profit_factor') or 0:>6.2f} "
            f"{metrics.get('win_rate', 0):>5.0%} {report['positive_windows']:>3d}/{report['window_count']:<4d} "
            f"{stress.get('total_return', 0):>7.1%} {benchmark.get('total_return', 0):>7.1%}"
        )
    summary_path = output_dir / "research_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    print(f"\nsummary -> {summary_path}")
    print("Paper-only research. Positive backtests do not guarantee future profits.")


if __name__ == "__main__":
    main()
