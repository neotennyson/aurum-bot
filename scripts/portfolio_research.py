#!/usr/bin/env python3
"""Fixed-parameter out-of-sample sleeve research and portfolio scaling study.

Sleeves: regime overlay (long-only 200-SMA hysteresis band) and weekly
time-series momentum (long-only). Parameters are fixed, not re-selected per
window, because the walk-forward study showed re-selection hurts out of sample.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars  # noqa: E402
from aurum.config import CostConfig, RiskConfig, RunConfig  # noqa: E402
from aurum.engine import BacktestEngine, performance_metrics  # noqa: E402
from aurum.models import EquityPoint  # noqa: E402

SLEEVES = {
    "regime": {
        "family": "regimeoverlay",
        "params": {"sma_period": 200, "exit_buffer_atr": 0.5, "allow_short": False, "stop_atr": 3.5},
    },
    "tsmom": {
        "family": "tsmom",
        "params": {"lookback": 63, "exit_lookback": 21, "regime_sma": 200, "allow_short": False, "stop_atr": 3.0},
    },
}
SCALES = (1.0, 1.5, 2.0, 2.5)


def base_config(symbol: str, family: str, capital: float, notional: float) -> RunConfig:
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
            risk_per_trade_pct=0.02,
            sizing_mode="notional",
            target_notional_pct=notional,
            max_notional_pct=3.0,
            margin_pct=0.15,
            max_margin_pct=0.6,
            max_quantity=250,
            multiplier=1.0,
            daily_loss_pct=0.10,
            max_trades_per_day=1,
            max_consecutive_losses=10,
            cooldown_bars=1,
        ),
    )


def stitched(bars, symbol, family, params, notional, capital, train_bars, test_bars, step_bars, pad=320):
    base = base_config(symbol, family, capital, notional)
    running = capital
    peak = running
    curve: list[EquityPoint] = []
    trades = []
    start = train_bars
    while start + test_bars <= len(bars):
        context = bars[max(0, start - pad) : start + test_bars]
        config = base.with_params(params)
        result = BacktestEngine(config).run(context, trade_from=bars[start].timestamp)
        factor = running / capital
        for point in result.equity_curve:
            running = point.equity * factor
            peak = max(peak, running)
            curve.append(EquityPoint(point.timestamp, running, running / peak - 1.0, point.price, point.position))
        trades.extend(result.trades)
        start += step_bars
    return curve, trades


def blend(curves, capital):
    length = min(len(curve) for curve in curves)
    out: list[EquityPoint] = []
    peak = capital
    for index in range(length):
        equity = sum(curve[index].equity / capital for curve in curves) / len(curves) * capital
        peak = max(peak, equity)
        out.append(EquityPoint(curves[0][index].timestamp, equity, equity / peak - 1.0, curves[0][index].price, 0))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-portfolio-research")
    parser.add_argument("--data", default="data/nifty_continuous_daily.csv")
    parser.add_argument("--symbol", default="48704")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--train", type=int, default=756)
    parser.add_argument("--test", type=int, default=252)
    parser.add_argument("--step", type=int, default=252)
    parser.add_argument("--output", default="reports/portfolio_research.json")
    args = parser.parse_args()

    bars = load_bars(args.data, args.symbol)
    report: dict = {
        "method": "fixed_parameters_rolling_oos",
        "note": (
            "Parameters fixed in advance (no per-window re-selection). Sleeve curves are "
            "concatenations of 1-year out-of-sample windows with a warmup pad, long-only."
        ),
        "symbol": args.symbol,
        "capital": args.capital,
        "sleeves": {},
        "combos": {},
        "scales": list(SCALES),
    }

    sleeve_curves: dict[str, list[EquityPoint]] = {}
    sleeve_trades: dict[str, list] = {}
    for name, spec in SLEEVES.items():
        curve, trades = stitched(
            bars,
            args.symbol,
            spec["family"],
            spec["params"],
            SCALES[0],
            args.capital,
            args.train,
            args.test,
            args.step,
        )
        sleeve_curves[name] = curve
        sleeve_trades[name] = trades
        metrics = performance_metrics(curve, trades, args.capital, 375)
        report["sleeves"][name] = {
            "family": spec["family"],
            "params": spec["params"],
            "metrics": metrics,
            "trades": len(trades),
            "trades_detail": [trade.to_dict() for trade in trades],
            "comment": (
                "Long-only regime overlay: participate above the 200-SMA, step aside below."
                if name == "regime"
                else "Weekly time-series momentum: hold while 3-month momentum is positive above the 200-SMA."
            ),
        }
        print(f"sleeve {name:<7} ret {metrics['total_return']:>7.1%} dd {metrics['max_drawdown']:>6.1%} sharpe {metrics['sharpe']:.2f} trades {len(trades)}")

    benchmark_curve = []
    peak = args.capital
    first_price = None
    for point in sleeve_curves["regime"]:
        if first_price is None:
            first_price = point.price
        equity = args.capital * point.price / first_price
        peak = max(peak, equity)
        benchmark_curve.append(EquityPoint(point.timestamp, equity, equity / peak - 1.0, point.price, 0))
    report["benchmark"] = {
        "metrics": performance_metrics(benchmark_curve, [], args.capital, 375),
        "curve": [point.to_dict() for point in benchmark_curve],
    }

    for scale in SCALES:
        curves = []
        for name, spec in SLEEVES.items():
            if scale == SCALES[0]:
                curves.append(sleeve_curves[name])
            else:
                curve, _ = stitched(
                    bars,
                    args.symbol,
                    spec["family"],
                    spec["params"],
                    scale,
                    args.capital,
                    args.train,
                    args.test,
                    args.step,
                )
                curves.append(curve)
        combo = blend(curves, args.capital)
        metrics = performance_metrics(combo, [], args.capital, 375)
        report["combos"][f"{scale:.1f}"] = {"metrics": metrics, "curve": [point.to_dict() for point in combo]}
        print(f"combo {scale:>4}x ret {metrics['total_return']:>7.1%} cagr {metrics['cagr']:>6.1%} "
              f"dd {metrics['max_drawdown']:>6.1%} sharpe {metrics['sharpe']:.2f} vol {metrics['annualized_volatility']:.1%}")

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(f"\nportfolio report -> {destination}")
    print("Paper-only research. Out-of-sample results are not a guarantee of future profits.")


if __name__ == "__main__":
    main()
