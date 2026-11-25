#!/usr/bin/env python3
"""Deployed portfolio research: regime 2.5x + stock sleeve (gated), tsmom observation only.

Both sleeves are chained from the same rolling one-year out-of-sample windows
(2019-04 onward, warmup pad). Capital is split 50/50 and rebalanced daily.
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
from aurum.config import CostConfig, RiskConfig, RunConfig  # noqa: E402
from aurum.engine import BacktestEngine, performance_metrics  # noqa: E402
from aurum.models import EquityPoint, Trade  # noqa: E402
from aurum.stocks import StockSleeveConfig, backtest_stock_sleeve, load_series  # noqa: E402

CAPITAL = 1_000_000.0
TRAIN, TEST, STEP, PAD = 756, 252, 252, 320
REGIME = {"sma_period": 200, "exit_buffer_atr": 0.5, "allow_short": False, "stop_atr": 3.5}
TSMOM = {"lookback": 63, "exit_lookback": 21, "regime_sma": 200, "allow_short": False, "stop_atr": 3.0}


def index_config(symbol: str, family: str, params: dict, notional: float) -> RunConfig:
    return RunConfig(
        symbol=symbol, timeframe="1d", bar_minutes=375, intraday_only=False, strategy=family, params=params,
        costs=CostConfig(),
        risk=RiskConfig(capital=CAPITAL, risk_per_trade_pct=0.02, sizing_mode="notional",
                        target_notional_pct=notional, max_notional_pct=3.0, margin_pct=0.15,
                        max_margin_pct=0.6, max_quantity=250, multiplier=1.0, daily_loss_pct=0.10,
                        max_trades_per_day=1, max_consecutive_losses=10, cooldown_bars=1),
    )


def stitch_index(bars, symbol: str, family: str, params: dict, notional: float) -> tuple[pd.Series, list]:
    running = CAPITAL
    peak = running
    curve: list[EquityPoint] = []
    trades = []
    start = TRAIN
    while start + TEST <= len(bars):
        context = bars[max(0, start - PAD): start + TEST]
        result = BacktestEngine(index_config(symbol, family, params, notional)).run(context, trade_from=bars[start].timestamp)
        factor = running / CAPITAL
        for point in result.equity_curve:
            running = point.equity * factor
            peak = max(peak, running)
            curve.append(EquityPoint(point.timestamp, running, running / peak - 1.0, point.price, point.position))
        trades.extend(result.trades)
        start += STEP
    series = pd.Series([p.equity for p in curve], index=[pd.Timestamp(p.timestamp).tz_localize(None) for p in curve])
    return series, trades


def stitch_stocks(stocks, market: pd.Series) -> tuple[pd.Series, list]:
    # window boundaries come from the index calendar so both sleeves share windows
    running = CAPITAL
    peak = running
    curve: list[EquityPoint] = []
    trades: list = []
    start = TRAIN
    calendar = sorted(market.index)
    while start + TEST <= len(calendar):
        window_start, window_end = calendar[start], calendar[start + TEST - 1]
        result = backtest_stock_sleeve(stocks, StockSleeveConfig(capital=CAPITAL), start=window_start,
                                       end=window_end, market_closes=market)
        factor = running / CAPITAL
        for point in result["equity_curve"]:
            running = point["equity"] * factor
            peak = max(peak, running)
            curve.append(EquityPoint(pd.Timestamp(point["timestamp"]).to_pydatetime(), running, running / peak - 1.0, 0.0, point["position"]))
        trades.extend(Trade.from_dict(item) for item in result["trades"])
        start += STEP
    series = pd.Series([p.equity for p in curve], index=[pd.Timestamp(p.timestamp).tz_localize(None) for p in curve])
    return series, trades


def blend(curves: list[pd.Series], weights: list[float]) -> pd.Series:
    joined = pd.concat(curves, axis=1, sort=True).ffill().dropna()
    returns = joined.pct_change().fillna(0.0)
    combined_returns = (returns * weights).sum(axis=1) / sum(weights)
    return CAPITAL * (1 + combined_returns).cumprod()


def curve_points(series: pd.Series) -> list[dict]:
    peak = CAPITAL
    points = []
    for timestamp, equity in series.items():
        peak = max(peak, equity)
        points.append(EquityPoint(timestamp.to_pydatetime(), float(equity), float(equity / peak - 1.0), 0.0, 0).to_dict())
    return points


def metrics(series: pd.Series, trades=None) -> dict:
    curve = [EquityPoint(pd.Timestamp(t).to_pydatetime(), float(v), 0.0, 0.0, 0) for t, v in series.items()]
    return performance_metrics(curve, trades or [], CAPITAL, 375)


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-portfolio-deployed")
    parser.add_argument("--index-data", default="data/nifty_continuous_daily.csv")
    parser.add_argument("--symbol", default="48704")
    parser.add_argument("--stocks-dir", default="data/stocks")
    parser.add_argument("--output", default="reports/portfolio_deployed.json")
    args = parser.parse_args()

    bars = load_bars(args.index_data, args.symbol)
    market = pd.Series([b.close for b in bars], index=[b.timestamp.date() for b in bars])
    stocks = load_series(args.stocks_dir)

    regime_curve, regime_trades = stitch_index(bars, args.symbol, "regimeoverlay", REGIME, 1.5)
    tsmom_curve, tsmom_trades = stitch_index(bars, args.symbol, "tsmom", TSMOM, 1.5)
    stock_curve, stock_trades = stitch_stocks(stocks, market)

    deployed = blend([regime_curve, stock_curve], [1, 1])
    deployed_with_tsmom = blend([regime_curve, stock_curve, tsmom_curve], [1, 1, 1])

    benchmark = pd.Series(
        [CAPITAL * value / market.iloc[0] for value in market.reindex(regime_curve.index).ffill().to_numpy()],
        index=regime_curve.index,
    )

    report = {
        "method": "deployed_portfolio_rolling_oos",
        "composition": {"regime": "NIFTY regime overlay 1.5x notional", "stocks": "gated stock momentum 1.0x",
                        "tsmom": "observation only (unfunded)", "capital_split": "50/50 regime vs stocks"},
        "window": {"first_test": str(regime_curve.index[0].date()), "last_test": str(regime_curve.index[-1].date()),
                   "train_bars": TRAIN, "test_bars": TEST},
        "note": (
            "tsmom demoted after failing 2022+ parameter robustness (median variant -8.2%). "
            "Deployed book is regime + stocks, 50/50 capital."
        ),
        "sleeves": {
            "regime": {"metrics": metrics(regime_curve, regime_trades), "trades": len(regime_trades), "curve": curve_points(regime_curve)},
            "stocks": {"metrics": metrics(stock_curve, stock_trades), "trades": len(stock_trades), "curve": curve_points(stock_curve)},
            "tsmom_observation": {"metrics": metrics(tsmom_curve, tsmom_trades), "trades": len(tsmom_trades)},
        },
        "deployed": {"metrics": metrics(deployed), "curve": curve_points(deployed)},
        "deployed_with_tsmom": {"metrics": metrics(deployed_with_tsmom)},
        "benchmark": {"metrics": metrics(benchmark), "curve": curve_points(benchmark)},
    }
    destination = Path(args.output)
    destination.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")

    for name in ("regime", "stocks", "tsmom_observation"):
        m = report["sleeves"][name]["metrics"]
        print(f"sleeve {name:<18} ret {m['total_return']:>+8.1%} cagr {m['cagr']:>+6.1%} dd {m['max_drawdown']:>+6.1%} sharpe {m['sharpe']:.2f}")
    for name in ("deployed", "deployed_with_tsmom", "benchmark"):
        m = report[name]["metrics"]
        print(f"{name:<20} ret {m['total_return']:>+8.1%} cagr {m['cagr']:>+6.1%} dd {m['max_drawdown']:>+6.1%} "
              f"sharpe {m['sharpe']:.2f} vol {m['annualized_volatility']:.1%}")
    print(f"\ndeployed report -> {destination}")


if __name__ == "__main__":
    main()
