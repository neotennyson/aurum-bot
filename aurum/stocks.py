"""Cross-sectional stock-F&O momentum sleeve.

Rules (fixed in advance, not tuned):
* Universe: liquid NSE stock futures with at least 300 daily bars.
* Signal each week on the previous trading day's close: eligible when
  close > 200-day SMA and 126-day return > 0; rank by 126-day return.
* Hold the top K; keep names while they stay inside rank 2K; exit otherwise.
* Equal notional per name, total exposure = config.exposure.
* Execute at the next session's open; whole shares; Indian F&O costs plus a
  small monthly roll-cost accrual.

This module backtests on continuous stock-futures data, so P&L is expressed in
futures terms. Execution research only - AURUM places no orders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .bars import Bar, load_bars
from .engine import performance_metrics
from .models import EquityPoint, Side, Trade


@dataclass
class StockSleeveConfig:
    lookback: int = 126
    regime_sma: int = 200
    top_k: int = 8
    keep_rank: int = 16
    exposure: float = 1.0
    capital: float = 1_000_000.0
    min_history: int = 260
    slippage_bps: float = 4.0
    roll_cost_bps_monthly: float = 3.0
    brokerage_per_order: float = 20.0
    brokerage_pct: float = 0.0003
    exchange_pct: float = 0.000019
    stt_sell_pct: float = 0.0002
    stamp_buy_pct: float = 0.00002
    sebi_pct: float = 0.000001
    gst_pct: float = 0.18
    allow_fractional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def adjust_for_corporate_actions(bars: list[Bar], threshold: float = 0.28) -> list[Bar]:
    """Back-adjust a continuous series whose level shifts on corporate actions.

    Dhan's continuous stock-futures history is stitched from unadjusted
    contracts, so split/bonus dates appear as one-day moves of -90%, +100% and
    similar. Walking backwards, every jump beyond ``threshold`` is treated as a
    scale change and earlier bars are rescaled to the latest price level.
    Isolated bad prints (jump that immediately reverts) are flattened to the
    previous close instead of being treated as a new scale.
    """
    if len(bars) < 3:
        return bars
    closes = [bar.close for bar in bars]
    adjustment = [1.0] * len(bars)
    cumulative = 1.0
    for index in range(len(bars) - 1, 0, -1):
        ratio = closes[index] / closes[index - 1]
        if ratio > 1.0 + threshold or ratio < 1.0 - threshold:
            cumulative *= ratio
        adjustment[index - 1] = cumulative
    adjusted = [
        Bar(
            timestamp=bar.timestamp,
            symbol=bar.symbol,
            open=bar.open * adjustment[index],
            high=bar.high * adjustment[index],
            low=bar.low * adjustment[index],
            close=bar.close * adjustment[index],
            volume=int(bar.volume * max(adjustment[index], 1e-9)),
            expiry=bar.expiry,
        )
        for index, bar in enumerate(bars)
    ]
    # flatten isolated remaining spikes (bad prints)
    cleaned: list[Bar] = []
    for index, bar in enumerate(adjusted):
        if 0 < index < len(adjusted) - 1:
            previous = cleaned[-1]
            forward = adjusted[index + 1]
            jump_back = abs(bar.close / previous.close - 1.0)
            jump_forward = abs(forward.close / bar.close - 1.0)
            if jump_back > threshold * 0.8 and jump_forward > threshold * 0.8 and forward.close / previous.close < 1.0 + 0.15:
                flat = Bar(
                    timestamp=bar.timestamp,
                    symbol=bar.symbol,
                    open=previous.close,
                    high=max(previous.close, bar.high),
                    low=min(previous.close, bar.low),
                    close=previous.close,
                    volume=bar.volume,
                    expiry=bar.expiry,
                )
                flat.validate()
                cleaned.append(flat)
                continue
        cleaned.append(bar)
    return cleaned


def load_series(directory: str | Path, min_history: int = 260, clean: bool = True) -> dict[str, list[Bar]]:
    series: dict[str, list[Bar]] = {}
    for path in sorted(Path(directory).glob("*.csv")):
        if path.name.startswith("_"):
            continue
        try:
            bars = load_bars(path, None)
        except ValueError:
            continue
        if len(bars) >= min_history:
            series[path.stem] = adjust_for_corporate_actions(bars) if clean else bars
    if not series:
        raise ValueError(f"no usable stock data in {directory}")
    return series


def frames(series: dict[str, list[Bar]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    opens = {}
    closes = {}
    for symbol, bars in series.items():
        index = [bar.timestamp.date() for bar in bars]
        opens[symbol] = pd.Series([bar.open for bar in bars], index=index)
        closes[symbol] = pd.Series([bar.close for bar in bars], index=index)
    return pd.DataFrame(opens).sort_index(), pd.DataFrame(closes).sort_index()


def _charges(config: StockSleeveConfig, buying: bool, price: float, quantity: float) -> float:
    turnover = abs(price * quantity)
    brokerage = min(config.brokerage_per_order, turnover * config.brokerage_pct)
    exchange = turnover * config.exchange_pct
    stt = 0.0 if buying else turnover * config.stt_sell_pct
    stamp = turnover * config.stamp_buy_pct if buying else 0.0
    sebi = turnover * config.sebi_pct
    gst = (brokerage + exchange + sebi) * config.gst_pct
    return brokerage + exchange + stt + stamp + sebi + gst


def _fill(config: StockSleeveConfig, price: float, buying: bool) -> float:
    slip = config.slippage_bps / 10_000.0
    return price * (1.0 + slip) if buying else price * (1.0 - slip)


def weekly_signal_dates(dates: list[date]) -> list[date]:
    """First trading day of each ISO week - the day a Friday close signal is traded."""
    seen: set[tuple[int, int]] = set()
    out: list[date] = []
    for day in dates:
        iso = day.isocalendar()
        key = (iso[0], iso[1])
        if key not in seen:
            seen.add(key)
            out.append(day)
    return out


def backtest_stock_sleeve(
    series: dict[str, list[Bar]],
    config: StockSleeveConfig | None = None,
    start: date | None = None,
    end: date | None = None,
    market_closes: pd.Series | None = None,
    market_sma: int = 200,
    selection_mode: str = "momentum",
    random_seed: int = 7,
    signal_lag: int = 1,
    lot_map: dict[str, int] | None = None,
    min_turnover_cr: float | None = None,
) -> dict[str, Any]:
    config = config or StockSleeveConfig()
    opens, closes = frames(series)
    sma = closes.rolling(config.regime_sma, min_periods=config.regime_sma).mean()
    momentum = closes.pct_change(config.lookback, fill_method=None)
    market_gate = None
    if market_closes is not None:
        market_gate = (market_closes > market_closes.rolling(market_sma, min_periods=market_sma).mean()).astype(float)
        market_gate[market_closes.rolling(market_sma, min_periods=market_sma).mean().isna()] = float("nan")
    turnover_median = None
    if min_turnover_cr:
        volumes = pd.DataFrame(
            {
                symbol: pd.Series([bar.volume for bar in bars], index=[bar.timestamp.date() for bar in bars])
                for symbol, bars in series.items()
            }
        )
        turnover_median = (closes * volumes).rolling(60, min_periods=45).median()
    dates = [day for day in closes.index if (start is None or day >= start) and (end is None or day <= end)]
    if len(dates) < 60:
        raise ValueError("not enough dates for the stock sleeve")
    rebalance_days = set(weekly_signal_dates(list(closes.index)))
    history_dates = list(closes.index)

    cash = config.capital
    positions: dict[str, dict[str, Any]] = {}
    trades: list[Trade] = []
    curve: list[EquityPoint] = []
    turnover_total = 0.0
    peak = config.capital
    last_mark: dict[str, float] = {}

    def mark_price(symbol: str, day: date) -> float | None:
        value = closes.at[day, symbol] if day in closes.index and symbol in closes.columns else None
        if value is not None and not pd.isna(value):
            last_mark[symbol] = float(value)
            return float(value)
        return last_mark.get(symbol)

    def close_position(symbol: str, day: date, price: float, reason: str) -> None:
        nonlocal cash, turnover_total
        entry = positions.pop(symbol)
        fill = _fill(config, price, buying=False)
        proceeds = fill * entry["quantity"]
        fees = _charges(config, False, fill, entry["quantity"])
        cash += proceeds - fees
        turnover_total += proceeds
        gross = (fill - entry["entry_price"]) * entry["quantity"]
        trades.append(
            Trade(
                symbol=symbol,
                side=Side.LONG,
                quantity=int(entry["quantity"]) if not config.allow_fractional else entry["quantity"],  # type: ignore[arg-type]
                entry_time=entry["entry_time"],
                exit_time=datetime.combine(day, datetime.min.time()),
                entry_price=entry["entry_price"],
                exit_price=fill,
                gross_pnl=gross,
                fees=entry["fees"] + fees,
                net_pnl=gross - entry["fees"] - fees,
                entry_reason=entry["reason"],
                exit_reason=reason,
                bars_held=max((day - entry["entry_time"].date()).days, 1),
                multiplier=1.0,
            )
        )

    def open_position(symbol: str, day: date, price: float, notional: float, signal_date: date) -> None:
        nonlocal cash, turnover_total
        fill = _fill(config, price, buying=True)
        quantity = notional / fill
        if not config.allow_fractional:
            quantity = float(int(quantity))
        lot = (lot_map or {}).get(symbol, 0) if lot_map else 0
        if lot and lot > 1:
            quantity = float(int(quantity) // lot * lot)
        if quantity <= 0:
            return
        cost = fill * quantity
        fees = _charges(config, True, fill, quantity)
        if cost + fees > cash:
            scale = (cash - fees) / cost
            quantity = quantity * scale if config.allow_fractional else float(int(quantity * scale))
            if quantity <= 0:
                return
            cost = fill * quantity
            fees = _charges(config, True, fill, quantity)
        cash -= cost + fees
        turnover_total += cost
        positions[symbol] = {
            "quantity": quantity,
            "entry_price": fill,
            "entry_time": datetime.combine(day, datetime.min.time()),
            "fees": fees,
            "reason": f"momentum_rank_{signal_date.isoformat()}",
        }

    import random as random_module

    rng = random_module.Random(random_seed)
    for day in dates:
        if day in rebalance_days:
            previous_days = [d for d in history_dates if d < day]
            if len(previous_days) < max(signal_lag, 1):
                continue
            signal_day = previous_days[-max(signal_lag, 1)]
            scores: list[tuple[float, str]] = []
            for symbol in closes.columns:
                close_value = closes.at[signal_day, symbol] if signal_day in closes.index else None
                sma_value = sma.at[signal_day, symbol] if signal_day in sma.index else None
                mom_value = momentum.at[signal_day, symbol] if signal_day in momentum.index else None
                if pd.isna(close_value) or pd.isna(sma_value) or pd.isna(mom_value):
                    continue
                if turnover_median is not None:
                    t_value = (
                        turnover_median.at[signal_day, symbol]
                        if signal_day in turnover_median.index and symbol in turnover_median.columns
                        else None
                    )
                    if t_value is None or pd.isna(t_value) or float(t_value) < min_turnover_cr * 1e7:
                        continue
                if close_value > sma_value and mom_value > 0:
                    scores.append((float(mom_value), symbol))
            scores.sort(reverse=True)
            ranked = [symbol for _, symbol in scores]
            if selection_mode == "random":
                rng.shuffle(ranked)
            elif selection_mode != "momentum":
                raise ValueError(f"unknown selection_mode {selection_mode!r}")
            if market_gate is not None:
                gate_value = market_gate.get(signal_day)
                if gate_value is None or pd.isna(gate_value) or gate_value == 0.0:
                    ranked = []
            top = set(ranked[: config.top_k])
            keep = [symbol for symbol in ranked[: config.keep_rank] if symbol in positions]
            target = top | set(keep)
            if len(target) > config.top_k + 4:
                target = set(ranked[: config.top_k + 4])
            # refresh marks for the whole universe before trading decisions
            for symbol in closes.columns:
                mark_price(symbol, day)
            # exits first
            for symbol in list(positions):
                if symbol not in target:
                    reference = None
                    if day in opens.index and symbol in opens.columns:
                        raw_open = opens.at[day, symbol]
                        if not pd.isna(raw_open):
                            reference = float(raw_open)
                    reference = reference if reference is not None else last_mark.get(symbol)
                    if reference is not None:
                        close_position(symbol, day, reference, "rebalance_exit")
            # entries to fill the book
            missing = [symbol for symbol in ranked if symbol in target and symbol not in positions]
            slots = max(config.top_k - len(positions), 0)
            equity_now = cash + sum(
                entry["quantity"] * last_mark.get(symbol, entry["entry_price"]) for symbol, entry in positions.items()
            )
            for symbol in missing[:slots]:
                if day in opens.index and symbol in opens.columns:
                    raw_open = opens.at[day, symbol]
                    if not pd.isna(raw_open):
                        notional = equity_now * config.exposure / config.top_k
                        open_position(symbol, day, float(raw_open), notional, signal_day)
            # roll cost accrual for held names (monthly cost, pro-rated weekly)
            for symbol, entry in positions.items():
                mark = last_mark.get(symbol, entry["entry_price"])
                cash -= entry["quantity"] * mark * (config.roll_cost_bps_monthly / 10_000.0) * (5.0 / 21.0)

        equity = cash
        for symbol, entry in positions.items():
            mark = mark_price(symbol, day)
            equity += entry["quantity"] * (mark if mark is not None else entry["entry_price"])
        peak = max(peak, equity)
        curve.append(
            EquityPoint(
                timestamp=datetime.combine(day, datetime.min.time()),
                equity=equity,
                drawdown=equity / peak - 1.0 if peak else 0.0,
                price=0.0,
                position=len(positions),
            )
        )

    # final liquidation at the last close for clean accounting
    last_day = dates[-1]
    for symbol in list(positions):
        reference = None
        if last_day in closes.index and symbol in closes.columns:
            raw = closes.at[last_day, symbol]
            if not pd.isna(raw):
                reference = float(raw)
        if reference is None:
            reference = last_mark.get(symbol)
        if reference is not None:
            close_position(symbol, last_day, reference, "end_of_data")
    result_metrics = performance_metrics(curve, trades, config.capital, 375)
    result_metrics["avg_holdings"] = sum(point.position for point in curve) / len(curve)
    return {
        "config": config.to_dict(),
        "metrics": result_metrics,
        "trades": [trade.to_dict() for trade in trades],
        "equity_curve": [point.to_dict() for point in curve],
        "turnover": turnover_total,
        "universe": sorted(series),
        "universe_size": len(series),
        "start": str(dates[0]),
        "end": str(dates[-1]),
    }


class StockPaperEngine:
    """Weekly stock sleeve as a persistent paper book. Paper only."""

    mode = "PAPER_ONLY"

    def __init__(self, config: StockSleeveConfig, state_path: str | Path) -> None:
        self.config = config
        self.state_path = Path(state_path)
        self.state = self._load()

    def _new_state(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "broker": "none (paper)",
            "strategy": "stock_momentum",
            "capital": self.config.capital,
            "cash": self.config.capital,
            "equity": self.config.capital,
            "positions": {},
            "trades": [],
            "equity_curve": [],
            "last_date": None,
            "last_week": None,
            "status": "waiting_for_data",
        }

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return self._new_state()

    def reset(self) -> None:
        self.state = self._new_state()
        self._persist()

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(self.state_path)

    def advance(self, day: date, opens: dict[str, float], closes: dict[str, float], signal_day: date | None,
                signal_close: dict[str, float], signal_sma: dict[str, float], signal_mom: dict[str, float],
                market_ok: bool = True) -> dict[str, Any]:
        """Process one completed session.

        ``opens``/``closes`` are prices for ``day``. The signal dictionaries
        describe the previous trading day (used only on rebalance days).
        """
        last_date = self.state.get("last_date")
        if last_date is not None and date.fromisoformat(last_date) >= day:
            return self.state
        positions: dict[str, dict[str, Any]] = self.state.get("positions", {})
        cash = float(self.state["cash"])
        config = self.config
        last_marks: dict[str, float] = self.state.get("last_marks", {})
        for symbol, value in closes.items():
            if value is not None:
                last_marks[symbol] = float(value)
        self.state["last_marks"] = last_marks
        mark = lambda symbol: last_marks.get(symbol)  # noqa: E731
        rebalance = False
        iso = day.isocalendar()
        week = f"{iso[0]}-{iso[1]:02d}"
        if self.state.get("last_week") != week:
            rebalance = True

        def close_position(symbol: str, price: float, reason: str) -> None:
            nonlocal cash
            entry = positions.pop(symbol)
            fill = _fill(config, price, buying=False)
            proceeds = fill * entry["quantity"]
            fees = _charges(config, False, fill, entry["quantity"])
            cash += proceeds - fees
            self.state["trades"].append(
                Trade(
                    symbol=symbol,
                    side=Side.LONG,
                    quantity=int(entry["quantity"]),
                    entry_time=datetime.fromisoformat(entry["entry_time"]),
                    exit_time=datetime.combine(day, datetime.min.time()),
                    entry_price=entry["entry_price"],
                    exit_price=fill,
                    gross_pnl=(fill - entry["entry_price"]) * entry["quantity"],
                    fees=entry["fees"] + fees,
                    net_pnl=(fill - entry["entry_price"]) * entry["quantity"] - entry["fees"] - fees,
                    entry_reason=entry["reason"],
                    exit_reason=reason,
                    bars_held=max((day - datetime.fromisoformat(entry["entry_time"]).date()).days, 1),
                    multiplier=1.0,
                ).to_dict()
            )

        def open_position(symbol: str, price: float, notional: float) -> None:
            nonlocal cash
            fill = _fill(config, price, buying=True)
            quantity = float(int(notional / fill))
            if quantity <= 0:
                return
            cost = fill * quantity
            fees = _charges(config, True, fill, quantity)
            if cost + fees > cash:
                quantity = float(int((cash - fees) / fill)) if cash > fees else 0.0
                if quantity <= 0:
                    return
                cost = fill * quantity
                fees = _charges(config, True, fill, quantity)
            cash -= cost + fees
            positions[symbol] = {
                "quantity": quantity,
                "entry_price": fill,
                "entry_time": datetime.combine(day, datetime.min.time()).isoformat(),
                "fees": fees,
                "reason": f"momentum_{signal_day.isoformat() if signal_day else 'na'}",
            }

        if rebalance and signal_day is not None:
            scores = []
            for symbol, close_value in signal_close.items():
                sma_value = signal_sma.get(symbol)
                mom_value = signal_mom.get(symbol)
                if sma_value is None or mom_value is None:
                    continue
                if close_value > sma_value and mom_value > 0:
                    scores.append((mom_value, symbol))
            scores.sort(reverse=True)
            ranked = [symbol for _, symbol in scores]
            if not market_ok:
                ranked = []
            top = set(ranked[: config.top_k])
            keep = [symbol for symbol in ranked[: config.keep_rank] if symbol in positions]
            target = top | set(keep)
            if len(target) > config.top_k + 4:
                target = set(ranked[: config.top_k + 4])
            for symbol in list(positions):
                if symbol not in target:
                    reference = opens.get(symbol) or mark(symbol)
                    if reference is not None:
                        close_position(symbol, reference, "rebalance_exit")
            missing = [symbol for symbol in ranked if symbol in target and symbol not in positions]
            equity_now = cash + sum(
                entry["quantity"] * (mark(symbol) or entry["entry_price"]) for symbol, entry in positions.items()
            )
            for symbol in missing[: max(config.top_k - len(positions), 0)]:
                if symbol in opens:
                    open_position(symbol, opens[symbol], equity_now * config.exposure / config.top_k)
            for symbol, entry in positions.items():
                value = mark(symbol) or entry["entry_price"]
                cash -= entry["quantity"] * value * (config.roll_cost_bps_monthly / 10_000.0) * (5.0 / 21.0)
            self.state["last_week"] = week

        equity = cash + sum(
            entry["quantity"] * (mark(symbol) or entry["entry_price"]) for symbol, entry in positions.items()
        )
        self.state.update(
            {
                "cash": cash,
                "equity": equity,
                "positions": positions,
                "last_date": day.isoformat(),
                "status": "invested" if positions else "flat",
            }
        )
        curve = self.state["equity_curve"]
        peak = max([float(self.state["capital"])] + [float(point["equity"]) for point in curve])
        peak = max(peak, equity)
        curve.append(
            EquityPoint(
                timestamp=datetime.combine(day, datetime.min.time()),
                equity=equity,
                drawdown=equity / peak - 1.0 if peak else 0.0,
                price=0.0,
                position=len(positions),
            ).to_dict()
        )
        self.state["metrics"] = self._metrics()
        self._persist()
        return self.state

    def _metrics(self) -> dict[str, Any]:
        curve = [EquityPoint.from_dict(item) for item in self.state.get("equity_curve", [])]
        trades = [Trade.from_dict(item) for item in self.state.get("trades", [])]
        return performance_metrics(curve, trades, self.config.capital, 375)


def save_report(report: dict[str, Any], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")


def load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
