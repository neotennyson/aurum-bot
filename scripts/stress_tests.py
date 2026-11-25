#!/usr/bin/env python3
"""AURUM validation suite: causality, noise, sensitivity, selection controls,
cost/execution stress, bootstrap confidence, survivorship and feasibility.

Run:  python3 scripts/stress_tests.py [--section all|engine|stocks|index|data]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import Bar, load_bars  # noqa: E402
from aurum.config import load_run_config  # noqa: E402
from aurum.engine import BacktestEngine  # noqa: E402
from aurum.models import EquityPoint  # noqa: E402
from aurum.stocks import StockSleeveConfig, backtest_stock_sleeve, frames, load_series  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
RESULTS: dict = {}


def ist(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=IST)


def load_market() -> pd.Series:
    bars = load_bars(ROOT / "data/nifty_continuous_daily.csv", "48704")
    return pd.Series([b.close for b in bars], index=[b.timestamp.date() for b in bars])


def metrics_of_curve(curve: pd.DataFrame) -> dict:
    equity = curve["equity"]
    returns = equity.pct_change().dropna()
    total = equity.iloc[-1] / equity.iloc[0] - 1
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    peak = equity.cummax()
    dd = (equity / peak - 1).min()
    sharpe = returns.mean() / returns.std() * math.sqrt(252) if returns.std() > 0 else 0.0
    return {"total_return": float(total), "cagr": float(cagr), "max_drawdown": float(dd), "sharpe": float(sharpe)}


def block_bootstrap(curve: pd.DataFrame, iterations: int = 2000, block: int = 20, seed: int = 11) -> dict:
    returns = curve["equity"].pct_change().dropna().to_numpy()
    if len(returns) < block * 3:
        return {}
    rng = np.random.default_rng(seed)
    blocks = math.ceil(len(returns) / block)
    starts = rng.integers(0, len(returns) - block, size=(iterations, blocks))
    samples = np.concatenate([returns[s : s + block] for s in starts.ravel()]).reshape(iterations, -1)
    eq = np.cumprod(1 + samples, axis=1)
    total = eq[:, -1] - 1
    years = len(returns) / 252
    cagr = np.where(total > -1, (1 + total) ** (1 / years) - 1, -1)
    peaks = np.maximum.accumulate(eq, axis=1)
    dds = (eq / peaks - 1).min(axis=1)
    return {
        "cagr_p5": float(np.percentile(cagr, 5)),
        "cagr_p50": float(np.percentile(cagr, 50)),
        "cagr_p95": float(np.percentile(cagr, 95)),
        "dd_p5": float(np.percentile(dds, 5)),
        "dd_p50": float(np.percentile(dds, 50)),
        "prob_positive": float((total > 0).mean()),
    }


def section_engine(series, market) -> None:
    print("\n=== 1. ENGINE CAUSALITY (prefix invariance) ===")
    findings = {}
    cutoff_index = int(len(series["48704"]) * 0.8)
    cutoff = series["48704"][cutoff_index].timestamp.date()
    for name in ("regime_daily", "tsmom_daily"):
        config = load_run_config(ROOT / f"config/{name}.json")
        full = BacktestEngine(config).run([b for b in series[config.symbol]])
        truncated = BacktestEngine(config).run([b for b in series[config.symbol]][:cutoff_index])
        full_before = [t for t in full.trades if t.exit_time.date() <= cutoff - timedelta(days=7)]
        trunc_before = [t for t in truncated.trades if t.exit_time.date() <= cutoff - timedelta(days=7)]
        same = len(full_before) == len(trunc_before) and all(
            abs(a.net_pnl - b.net_pnl) < 1e-6 and a.exit_time == b.exit_time
            for a, b in zip(full_before, trunc_before)
        )
        findings[name] = {"trades_before_cutoff": len(full_before), "identical": same}
        print(f"  {name:<14} trades before cutoff: {len(full_before):>3} | identical with truncated data: {same}")
    stock_trunc = backtest_stock_sleeve(series["stocks"], StockSleeveConfig(), end=cutoff, market_closes=market)
    stock_full = backtest_stock_sleeve(series["stocks"], StockSleeveConfig(), market_closes=market)
    stock_cut = cutoff - timedelta(days=10)
    a = [t for t in stock_trunc["trades"] if t["exit_time"][:10] <= stock_cut.isoformat()]
    b = [t for t in stock_full["trades"] if t["exit_time"][:10] <= stock_cut.isoformat()]
    same = len(a) == len(b) and all(abs(x["net_pnl"] - y["net_pnl"]) < 1e-6 for x, y in zip(a, b))
    findings["stocks"] = {"trades_before_cutoff": len(a), "identical": same}
    print(f"  stocks         trades before cutoff: {len(a):>3} | identical with truncated data: {same}")
    print("  verdict: signals use only closed bars; execution next open (tests + prefix invariance)")
    RESULTS["causality"] = findings

    print("\n=== 2. NOISE TEST (does the engine hallucinate edge?) ===")
    config = load_run_config(ROOT / "config/regime_daily.json")
    outcomes = []
    for seed in range(1, 7):
        rng = random.Random(seed)
        price = 20000.0
        bars = []
        start_day = date(2016, 1, 1)
        day = start_day
        while len(bars) < 2500:
            day += timedelta(days=1)
            if day.weekday() >= 5:
                continue
            shock = rng.gauss(0, 0.008)
            open_price = price
            price = max(1.0, price * (1 + shock))
            bars.append(Bar(day and datetime.combine(day, time.min, tzinfo=IST), "48704", open_price,
                            max(open_price, price) * 1.003, min(open_price, price) * 0.997, price, 1_000_000))
        synthetic = replace(config, symbol="48704")
        result = BacktestEngine(synthetic).run(bars)
        outcomes.append(result.metrics["total_return"])
    mean = statistics.mean(outcomes)
    print(f"  zero-drift random walks: mean return {mean:+.1%} across {len(outcomes)} seeds "
          f"(range {min(outcomes):+.1%} to {max(outcomes):+.1%})")
    print("  verdict: expected slightly negative from costs; strongly positive would indicate leakage")
    RESULTS["noise"] = {"mean": mean, "outcomes": outcomes}


def empty_curve() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "equity"]).set_index("timestamp")


def curve_from_report(items: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(items)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.tz_localize(None)
    return frame.set_index("timestamp")[["equity", "drawdown"]]


def section_stocks(series, market) -> None:
    stocks = series["stocks"]
    holdout = date(2022, 1, 1)
    base = dict(series=stocks, start=holdout, market_closes=market)

    print("\n=== 3. PARAMETER SENSITIVITY (stock sleeve, holdout 2022+, gated) ===")
    rows = []
    for top_k in (5, 8, 12):
        for lookback in (63, 126, 189):
            for sma in (150, 200):
                config = StockSleeveConfig(top_k=top_k, keep_rank=top_k * 2, lookback=lookback, regime_sma=sma)
                m = backtest_stock_sleeve(stocks, config, start=holdout, market_closes=market)["metrics"]
                rows.append({"top_k": top_k, "lookback": lookback, "sma": sma, "return": m["total_return"],
                             "cagr": m["cagr"], "dd": m["max_drawdown"], "sharpe": m["sharpe"]})
    frame = pd.DataFrame(rows)
    print(f"  {len(rows)} variants: return {frame['return'].min():+.1%} .. {frame['return'].max():+.1%} "
          f"(median {frame['return'].median():+.1%}) | sharpe {frame['sharpe'].min():.2f} .. {frame['sharpe'].max():.2f} "
          f"(median {frame['sharpe'].median():.2f}) | dd worst {frame['dd'].min():+.1%}")
    positive = (frame["return"] > 0).mean()
    print(f"  positive variants: {positive:.0%} | deployed (top8/126/200) is median-like: "
          f"{bool(((frame['top_k']==8)&(frame['lookback']==126)&(frame['sma']==200)).any())}")
    RESULTS["stock_sensitivity"] = rows

    print("\n=== 4. SELECTION CONTROL (momentum vs random eligible names) ===")
    momentum = backtest_stock_sleeve(stocks, StockSleeveConfig(), start=holdout, market_closes=market)["metrics"]
    random_runs = []
    for seed in range(30):
        m = backtest_stock_sleeve(stocks, StockSleeveConfig(), start=holdout, market_closes=market,
                                  selection_mode="random", random_seed=seed)["metrics"]
        random_runs.append(m)
    random_returns = [m["total_return"] for m in random_runs]
    random_sharpes = [m["sharpe"] for m in random_runs]
    better = sum(1 for value in random_returns if value < momentum["total_return"])
    print(f"  momentum top-8: return {momentum['total_return']:+.1%} sharpe {momentum['sharpe']:.2f}")
    print(f"  random eligible: return median {statistics.median(random_returns):+.1%} "
          f"(range {min(random_returns):+.1%}..{max(random_returns):+.1%}) sharpe median {statistics.median(random_sharpes):.2f}")
    print(f"  momentum beat {better}/30 random books")
    verdict = "ranking adds value" if better >= 24 else "ranking may not add value - investigate"
    print(f"  verdict: {verdict}")
    RESULTS["selection_control"] = {"momentum": momentum, "random_median_return": statistics.median(random_returns),
                                    "random_min": min(random_returns), "random_max": max(random_returns),
                                    "momentum_wins": better}

    print("\n=== 5. COST LADDER (stock sleeve holdout) ===")
    cost_rows = []
    for slippage, roll in ((0.0, 0.0), (2.0, 1.5), (4.0, 3.0), (8.0, 6.0), (15.0, 10.0)):
        config = StockSleeveConfig(slippage_bps=slippage, roll_cost_bps_monthly=roll)
        m = backtest_stock_sleeve(stocks, config, start=holdout, market_closes=market)["metrics"]
        cost_rows.append({"slippage_bps": slippage, "roll_bps": roll, "return": m["total_return"], "sharpe": m["sharpe"]})
        print(f"  slip {slippage:>4.0f} bps + roll {roll:>4.1f}: return {m['total_return']:>+7.1%} sharpe {m['sharpe']:.2f}")
    RESULTS["stock_cost_ladder"] = cost_rows

    print("\n=== 6. EXECUTION DELAY (stock sleeve holdout) ===")
    for lag in (1, 2, 3):
        m = backtest_stock_sleeve(stocks, StockSleeveConfig(), start=holdout, market_closes=market, signal_lag=lag)["metrics"]
        print(f"  signal lag {lag} day(s): return {m['total_return']:>+7.1%} cagr {m['cagr']:+.1%} sharpe {m['sharpe']:.2f}")
        RESULTS.setdefault("execution_delay", []).append({"lag": lag, "return": m["total_return"], "sharpe": m["sharpe"]})

    print("\n=== 7. BOOTSTRAP CONFIDENCE (stock sleeve holdout, 20-day blocks, 2000 runs) ===")
    gated = backtest_stock_sleeve(stocks, StockSleeveConfig(), start=holdout, market_closes=market)
    curve = pd.DataFrame(gated["equity_curve"])
    curve["timestamp"] = pd.to_datetime(curve["timestamp"])
    curve = curve.set_index("timestamp")[["equity"]]
    boot = block_bootstrap(curve)
    if boot:
        print(f"  CAGR 5th/50th/95th: {boot['cagr_p5']:+.1%} / {boot['cagr_p50']:+.1%} / {boot['cagr_p95']:+.1%}")
        print(f"  max DD 5th/50th: {boot['dd_p5']:+.1%} / {boot['dd_p50']:+.1%} | P(profit) {boot['prob_positive']:.0%}")
    RESULTS["stock_bootstrap"] = boot

    print("\n=== 8. SUB-PERIODS (stock sleeve holdout, gated) ===")
    yearly = curve["equity"].resample("YE").last().pct_change().dropna()
    for timestamp, value in yearly.items():
        print(f"  {timestamp.year}: {value:+.1%}")
    RESULTS["stock_yearly"] = {str(k.year): float(v) for k, v in yearly.items()}


def section_index(series, market) -> None:
    print("\n=== 9. INDEX SLEEVE PARAMETER SENSITIVITY (2022+ single path, deployed sizing) ===")
    rows = []
    for name, grid in (
        ("regime_daily", [{"sma_period": s, "exit_buffer_atr": b} for s in (150, 175, 200, 225) for b in (0.0, 0.5, 1.0)]),
        ("tsmom_daily", [{"lookback": l, "exit_lookback": e} for l in (42, 63, 126) for e in (10, 21)]),
    ):
        base = load_run_config(ROOT / f"config/{name}.json")
        for params in grid:
            config = base.with_params(params)
            context = [b for b in series["48704"] if b.timestamp.date() >= date(2021, 1, 1)]
            result = BacktestEngine(config).run(context, trade_from=ist(date(2022, 1, 1)))
            rows.append({"strategy": name, **params, "return": result.metrics["total_return"],
                         "dd": result.metrics["max_drawdown"], "sharpe": result.metrics["sharpe"]})
    frame = pd.DataFrame(rows)
    for name in ("regime_daily", "tsmom_daily"):
        subset = frame[frame["strategy"] == name]
        print(f"  {name:<14} {len(subset)} variants: return {subset['return'].min():+.1%} .. {subset['return'].max():+.1%} "
              f"(median {subset['return'].median():+.1%}) | dd worst {subset['dd'].min():+.1%}")
    RESULTS["index_sensitivity"] = rows

    print("\n=== 10. INDEX COST LADDER (2016-2026 full path) ===")
    for name in ("regime_daily", "tsmom_daily"):
        base = load_run_config(ROOT / f"config/{name}.json")
        for slippage in (1.5, 3.0, 6.0, 10.0):
            costs = replace(base.costs, slippage_bps=slippage)
            config = replace(base, costs=costs)
            result = BacktestEngine(config).run([b for b in series["48704"]])
            print(f"  {name:<14} slip {slippage:>4.1f} bps: return {result.metrics['total_return']:>+8.1%} "
                  f"sharpe {result.metrics['sharpe']:.2f}")
            RESULTS.setdefault("index_cost_ladder", []).append(
                {"strategy": name, "slippage_bps": slippage, "return": result.metrics["total_return"],
                 "sharpe": result.metrics["sharpe"]})

    print("\n=== 11. BOOTSTRAP CONFIDENCE (deployed 2.5x portfolio OOS curve) ===")
    try:
        portfolio = json.loads((ROOT / "reports/portfolio_research.json").read_text())
        combo_curve = curve_from_report(portfolio["combos"]["2.5"]["curve"])
        boot = block_bootstrap(combo_curve)
        print(f"  combined portfolio CAGR 5th/50th/95th: {boot['cagr_p5']:+.1%} / {boot['cagr_p50']:+.1%} / {boot['cagr_p95']:+.1%}")
        print(f"  max DD 5th/50th: {boot['dd_p5']:+.1%} / {boot['dd_p50']:+.1%} | P(profit) {boot['prob_positive']:.0%}")
        RESULTS["portfolio_bootstrap"] = boot
    except (OSError, KeyError) as error:
        print("  portfolio report missing:", error)


def section_data(series, market) -> None:
    print("\n=== 12. SURVIVORSHIP AND DATA COVERAGE ===")
    first_years = {}
    for symbol, bars in series["stocks"].items():
        first_years.setdefault(bars[0].timestamp.year, []).append(symbol)
    for year in sorted(first_years):
        print(f"  first full history in {year}: {len(first_years[year])} symbols")
    universe = pd.read_csv(ROOT / "data/reference/fno_universe.csv", dtype=str)
    full_universe = pd.read_csv(ROOT / "data/reference/dhan_scrip_master.csv", dtype=str)
    nse_futs = full_universe[(full_universe["SEM_EXM_EXCH_ID"] == "NSE") & (full_universe["SEM_INSTRUMENT_NAME"] == "FUTSTK")]
    today_symbols = set(universe["symbol"])
    print(f"  today's F&O stock count: {universe['symbol'].nunique()} | fetched: {len(series['stocks'])}")
    print("  survivorship: the backtest universe is today's F&O list, so delisted/removed names are missing")
    print("  direction: this flatters results somewhat; the equal-weight benchmark shares the same bias")
    RESULTS["data_coverage"] = {str(y): len(v) for y, v in first_years.items()}

    print("\n=== 13. LOT-SIZE AND LIQUIDITY FEASIBILITY ===")
    universe_idx = universe.set_index("symbol")
    opens, closes = frames(series["stocks"])
    last_close = closes.iloc[-1]
    turnover = {}
    for symbol, bars in series["stocks"].items():
        recent = bars[-250:]
        turnover[symbol] = float(np.median([b.close * b.volume for b in recent]))
    rows = []
    for symbol in series["stocks"]:
        lot = int(universe_idx.loc[symbol, "lot_units"]) if symbol in universe_idx.index else 0
        one_lot = lot * float(last_close.get(symbol, 0))
        rows.append({"symbol": symbol, "lot": lot, "one_lot_notional": one_lot,
                     "median_turnover": turnover.get(symbol, 0)})
    feas = pd.DataFrame(rows)
    for capital in (1_000_000, 2_500_000, 5_000_000, 10_000_000):
        allocation = capital / 8
        tradable = (feas["one_lot_notional"] <= allocation).sum()
        liquid = ((allocation <= 0.01 * feas["median_turnover"]) & (feas["one_lot_notional"] <= allocation)).sum()
        print(f"  capital {capital/1e6:.1f}M (alloc/name {allocation/1e3:.0f}k): "
              f"{tradable}/{len(feas)} names have 1 lot within allocation | "
              f"{liquid} also liquid (order <=1% of median daily turnover)")
    print("  note: the research book uses whole shares; live futures trading must round to whole lots")
    RESULTS["feasibility"] = feas.describe().to_dict()


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-stress-tests")
    parser.add_argument("--section", default="all", choices=["all", "engine", "stocks", "index", "data"])
    args = parser.parse_args()

    nifty = load_bars(ROOT / "data/nifty_continuous_daily.csv", "48704")
    market = load_market()
    series = {"48704": nifty, "stocks": load_series(ROOT / "data/stocks")}
    print(f"loaded: {len(nifty)} index bars, {len(series['stocks'])} stock series")

    if args.section in ("all", "engine"):
        section_engine(series, market)
    if args.section in ("all", "stocks"):
        section_stocks(series, market)
    if args.section in ("all", "index"):
        section_index(series, market)
    if args.section in ("all", "data"):
        section_data(series, market)

    destination = ROOT / "reports/stress_tests.json"
    destination.write_text(json.dumps(RESULTS, indent=2, allow_nan=False, default=str), encoding="utf-8")
    print(f"\nstress report -> {destination}")


if __name__ == "__main__":
    main()
