"""Rolling walk-forward research harness.

For every out-of-sample window the parameters are chosen *only* on the
preceding training window, then applied unchanged to the unseen test window.
Test windows are contiguous, so the stitched out-of-sample equity curve is a
fair picture of how the rule would have behaved in real time.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from .bars import Bar
from .config import CostConfig, RunConfig
from .engine import BacktestEngine, performance_metrics
from .models import EquityPoint, Trade


def default_grid(family: str) -> list[dict[str, Any]]:
    if family == "donchian":
        return [
            {
                "entry": entry,
                "exit": exit_lookback,
                "regime_sma": regime,
                "stop_atr": stop,
                "allow_short": allow_short,
                "target_r": 100.0,
            }
            for entry in (20, 40, 55)
            for exit_lookback in (10, 20)
            for regime in (100, 200)
            for stop in (2.0, 3.0)
            for allow_short in (True, False)
        ]
    if family == "pullback":
        return [
            {
                "rsi_period": period,
                "rsi_level": level,
                "ema_period": ema_period,
                "regime_sma": 200,
                "stop_atr": stop,
                "target_r": 3.0,
            }
            for period in (2, 3)
            for level in (5.0, 10.0, 15.0, 20.0)
            for ema_period in (10, 20)
            for stop in (2.0, 2.5)
        ]
    if family == "tsmom":
        return [
            {
                "lookback": lookback,
                "exit_lookback": exit_lookback,
                "regime_sma": regime,
                "allow_short": allow_short,
                "threshold": threshold,
                "stop_atr": 3.0,
            }
            for lookback in (42, 63, 126)
            for exit_lookback in (10, 21)
            for regime in (0, 200)
            for allow_short in (True, False)
            for threshold in (0.0, 0.02)
        ]
    if family == "orb":
        return [
            {"or_bars": or_bars, "target_r": target, "stop_atr": stop}
            for or_bars in (2, 3)
            for target in (1.0, 1.5)
            for stop in (0.4, 0.6)
        ]
    if family == "regimeoverlay":
        return [
            {
                "sma_period": sma_period,
                "exit_buffer_atr": out_buffer,
                "allow_short": allow_short,
                "stop_atr": stop,
            }
            for sma_period in (100, 150, 200)
            for out_buffer in (0.0, 0.5, 1.0)
            for allow_short in (True, False)
            for stop in (3.5,)
        ]
    raise ValueError(f"no grid defined for {family!r}")


def _score(metrics: dict[str, Any], minimum_trades: int) -> float:
    if metrics.get("trades", 0) < minimum_trades:
        return float("-inf")
    return float(
        metrics.get("total_return", -1.0)
        + 0.25 * metrics.get("sharpe", 0.0)
        - 0.5 * abs(metrics.get("max_drawdown", 0.0))
    )


def _stress_costs(costs: CostConfig, slippage_multiple: float = 2.0, fee_multiple: float = 1.5) -> CostConfig:
    return replace(
        costs,
        slippage_bps=costs.slippage_bps * slippage_multiple,
        brokerage_pct=costs.brokerage_pct * fee_multiple,
        exchange_pct=costs.exchange_pct * fee_multiple,
        stt_sell_pct=costs.stt_sell_pct * fee_multiple,
        stamp_buy_pct=costs.stamp_buy_pct * fee_multiple,
        sebi_pct=costs.sebi_pct * fee_multiple,
    )


WARMUP_PAD_BARS = 320


def run_walk_forward(
    bars: list[Bar],
    base: RunConfig,
    grid: list[dict[str, Any]],
    train_bars: int = 756,
    test_bars: int = 252,
    step_bars: int | None = None,
    minimum_train_trades: int | None = None,
) -> dict[str, Any]:
    data = sorted((bar for bar in bars if bar.symbol == base.symbol), key=lambda bar: bar.timestamp)
    step = step_bars or test_bars
    if len(data) < train_bars + test_bars:
        raise ValueError("not enough bars for walk-forward validation")
    if minimum_train_trades is None:
        minimum_train_trades = max(6, train_bars // 75)

    windows: list[dict[str, Any]] = []
    oos_curve: list[EquityPoint] = []
    benchmark_curve: list[EquityPoint] = []
    oos_trades: list[Trade] = []
    running_equity = base.risk.capital
    running_benchmark = base.risk.capital
    peak = running_equity
    benchmark_peak = running_benchmark
    selection_counts: dict[str, int] = {}
    stress_curve: list[EquityPoint] = []
    stress_running = base.risk.capital
    stress_peak = stress_running

    start = train_bars
    window_index = 0
    while start + test_bars <= len(data):
        train = data[max(0, start - train_bars) : start]
        test = data[start : start + test_bars]
        best_params: dict[str, Any] | None = None
        best_score = float("-inf")
        best_train_metrics: dict[str, Any] = {}
        for params in grid:
            try:
                result = BacktestEngine(base.with_params(params)).run(
                    data[max(0, start - train_bars - WARMUP_PAD_BARS) : start],
                    trade_from=train[0].timestamp,
                )
            except ValueError:
                continue
            score = _score(result.metrics, minimum_train_trades)
            if score > best_score:
                best_score = score
                best_params = params
                best_train_metrics = result.metrics
        if best_params is None:
            start += step
            window_index += 1
            continue
        key = json.dumps(best_params, sort_keys=True, default=str)
        selection_counts[key] = selection_counts.get(key, 0) + 1

        selected_config = base.with_params(best_params)
        context = data[max(0, start - WARMUP_PAD_BARS) : start + test_bars]
        test_result = BacktestEngine(selected_config).run(context, trade_from=test[0].timestamp)
        test_metrics = test_result.metrics

        factor = running_equity / base.risk.capital
        for point in test_result.equity_curve:
            running_equity = point.equity * factor
            peak = max(peak, running_equity)
            oos_curve.append(
                EquityPoint(point.timestamp, running_equity, running_equity / peak - 1.0, point.price, point.position)
            )
        benchmark_factor = running_benchmark / base.risk.capital
        first_price = test_result.equity_curve[0].price if test_result.equity_curve else 0.0
        for point in test_result.equity_curve:
            benchmark_equity = benchmark_factor * base.risk.capital * (point.price / first_price) if first_price else running_benchmark
            running_benchmark = benchmark_equity
            benchmark_peak = max(benchmark_peak, running_benchmark)
            benchmark_curve.append(
                EquityPoint(
                    point.timestamp,
                    running_benchmark,
                    running_benchmark / benchmark_peak - 1.0,
                    point.price,
                    0,
                )
            )
        for trade in test_result.trades:
            oos_trades.append(trade)

        stressed = replace(
            selected_config,
            costs=_stress_costs(selected_config.costs),
        )
        stress_result = BacktestEngine(stressed).run(context, trade_from=test[0].timestamp)
        stress_factor = stress_running / base.risk.capital
        for point in stress_result.equity_curve:
            stress_running = point.equity * stress_factor
            stress_peak = max(stress_peak, stress_running)
            stress_curve.append(
                EquityPoint(point.timestamp, stress_running, stress_running / stress_peak - 1.0, point.price, point.position)
            )

        windows.append(
            {
                "window": window_index,
                "train_start": train[0].timestamp.isoformat(),
                "train_end": train[-1].timestamp.isoformat(),
                "test_start": test[0].timestamp.isoformat(),
                "test_end": test[-1].timestamp.isoformat(),
                "selected_params": best_params,
                "train_score": best_score if best_score != float("-inf") else None,
                "train_metrics": _thin(best_train_metrics),
                "test_metrics": _thin(test_metrics),
                "test_stress_metrics": _thin(stress_result.metrics),
            }
        )
        start += step
        window_index += 1

    if not oos_curve:
        raise ValueError("walk-forward produced no out-of-sample windows")
    oos_metrics = performance_metrics(oos_curve, oos_trades, base.risk.capital, base.bar_minutes)
    benchmark_metrics = performance_metrics(benchmark_curve, [], base.risk.capital, base.bar_minutes)
    stress_metrics = performance_metrics(stress_curve, [], base.risk.capital, base.bar_minutes)
    positive = sum(1 for window in windows if window["test_metrics"].get("total_return", -1.0) > 0)
    return {
        "method": "rolling_walk_forward",
        "strategy": base.strategy,
        "grid_size": len(grid),
        "train_bars": train_bars,
        "test_bars": test_bars,
        "step_bars": step,
        "windows": windows,
        "window_count": len(windows),
        "positive_windows": positive,
        "selection_counts": selection_counts,
        "oos_metrics": oos_metrics,
        "benchmark_metrics": benchmark_metrics,
        "oos_stress_metrics": stress_metrics,
        "oos_return": oos_metrics.get("total_return"),
        "benchmark_return": benchmark_metrics.get("total_return"),
        "equity_curve": [point.to_dict() for point in oos_curve],
        "benchmark_curve": [point.to_dict() for point in benchmark_curve],
        "stress_curve": [point.to_dict() for point in stress_curve],
        "trades": [trade.to_dict() for trade in oos_trades],
        "config": base.to_dict(),
        "disclaimer": (
            "Walk-forward out-of-sample research on paper only. Positive results are not a "
            "guarantee of future profit; costs, slippage and regime change can erase an edge."
        ),
    }


def _thin(metrics: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "total_return",
        "cagr",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "trades",
        "expectancy",
        "total_fees",
        "benchmark_return",
    )
    return {key: metrics.get(key) for key in keep if key in metrics}
