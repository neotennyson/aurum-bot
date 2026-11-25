#!/usr/bin/env python3
"""Quarterly robustness review for AURUM.

Re-runs the validation gates that matter for capital allocation:
* regime overlay parameter range (must stay mostly positive to keep funding),
* weekly momentum parameter range (must recover to earn capital back),
* gated stock sleeve holdout performance.

Promotion criteria for tsmom are declared here, before looking at results:
  median variant return > 0 AND at least 60% of variants positive AND
  the deployed parameterisation itself positive over 2022+.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars  # noqa: E402
from aurum.config import load_run_config  # noqa: E402
from aurum.engine import BacktestEngine  # noqa: E402
from aurum.stocks import StockSleeveConfig, backtest_stock_sleeve, load_series  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def evaluate_index(config_path: Path, symbol: str, grid: list[dict], bars, window_start: date) -> dict:
    base = load_run_config(config_path)
    results = []
    for params in grid:
        config = base.with_params(params)
        context = [b for b in bars if b.timestamp.date() >= date(window_start.year - 1, 1, 1)]
        result = BacktestEngine(config).run(context, trade_from=__import__("datetime").datetime.combine(window_start, __import__("datetime").time.min, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata")))
        results.append({"params": params, "return": result.metrics["total_return"],
                        "dd": result.metrics["max_drawdown"], "sharpe": result.metrics["sharpe"]})
    returns = [item["return"] for item in results]
    median = sorted(returns)[len(returns) // 2]
    positive = sum(1 for value in returns if value > 0) / len(returns)
    deployed_params = dict(base.params)
    deployed = next((item for item in results if item["params"] == {k: deployed_params[k] for k in item["params"]}), None)
    return {"variants": results, "median_return": median, "positive_share": positive,
            "deployed_return": deployed["return"] if deployed else None}


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-quarterly-check")
    parser.add_argument("--output", default="reports/quarterly_check.json")
    parser.add_argument("--window-start", default="2022-01-01")
    args = parser.parse_args()

    window_start = date.fromisoformat(args.window_start)
    bars = load_bars(ROOT / "data/nifty_continuous_daily.csv", "48704")
    market = pd.Series([b.close for b in bars], index=[b.timestamp.date() for b in bars])
    stocks = load_series(ROOT / "data/stocks")

    regime_grid = [{"sma_period": s, "exit_buffer_atr": b} for s in (150, 175, 200, 225) for b in (0.0, 0.5, 1.0)]
    tsmom_grid = [{"lookback": l, "exit_lookback": e} for l in (42, 63, 126) for e in (10, 21)]

    regime = evaluate_index(ROOT / "config/regime_daily.json", "48704", regime_grid, bars, window_start)
    tsmom = evaluate_index(ROOT / "config/tsmom_daily.json", "48704", tsmom_grid, bars, window_start)
    stock = backtest_stock_sleeve(stocks, StockSleeveConfig(), start=window_start, market_closes=market)["metrics"]

    tsmom_promote = (
        tsmom["median_return"] > 0
        and tsmom["positive_share"] >= 0.60
        and (tsmom["deployed_return"] is not None and tsmom["deployed_return"] > 0)
    )
    regime_confirmed = regime["median_return"] > 0 and regime["positive_share"] >= 0.60

    report = {
        "as_of": date.today().isoformat(),
        "window_start": args.window_start,
        "regime": {**regime, "status": "confirmed_funded" if regime_confirmed else "warning_review"},
        "tsmom": {**tsmom, "status": "eligible_for_promotion" if tsmom_promote else "stay_observation"},
        "stocks": {"return": stock["total_return"], "cagr": stock["cagr"], "dd": stock["max_drawdown"],
                   "sharpe": stock["sharpe"], "status": "funded"},
        "criteria": {
            "tsmom_promotion": "median variant > 0, >=60% variants positive, deployed params positive",
            "regime_funding": "median variant > 0, >=60% variants positive",
        },
    }
    destination = ROOT / args.output
    destination.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    history = ROOT / "reports/quarterly_history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"as_of": report["as_of"], "regime": report["regime"]["status"],
                                 "tsmom": report["tsmom"]["status"],
                                 "stocks_return": stock["total_return"]}, default=str) + "\n")

    print(f"AURUM quarterly check {report['as_of']} (window {args.window_start}+)")
    print(f"  regime: median {regime['median_return']:+.1%}, positive {regime['positive_share']:.0%} -> {report['regime']['status']}")
    print(f"  tsmom:  median {tsmom['median_return']:+.1%}, positive {tsmom['positive_share']:.0%} -> {report['tsmom']['status']}")
    print(f"  stocks: return {stock['total_return']:+.1%}, dd {stock['max_drawdown']:+.1%}, sharpe {stock['sharpe']:.2f}")
    print(f"\nreport -> {destination}")


if __name__ == "__main__":
    main()
