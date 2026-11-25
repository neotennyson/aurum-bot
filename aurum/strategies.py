"""AURUM strategy library.

Every strategy is a causal function of the bars it is shown: signals are
computed from closed bars only and the engine fills them on the next bar.
"""

from __future__ import annotations

from datetime import time
from typing import Any

from .bars import Bar, session_time
from .config import RunConfig
from .indicators import atr, ema, pct_change, rsi, sma
from .models import Position, Side, Signal

DEFAULTS: dict[str, dict[str, Any]] = {
    "donchian": {
        "entry": 20,
        "exit": 10,
        "regime_sma": 200,
        "atr_period": 14,
        "stop_atr": 2.0,
        "target_r": 100.0,
        "allow_short": True,
        "max_holding_bars": 250,
    },
    "pullback": {
        "rsi_period": 2,
        "rsi_level": 10.0,
        "rsi_exit": 70.0,
        "ema_period": 20,
        "ema_atr": 0.75,
        "regime_sma": 200,
        "atr_period": 14,
        "stop_atr": 2.5,
        "target_r": 3.0,
        "allow_short": False,
        "max_holding_bars": 15,
    },
    "tsmom": {
        "lookback": 63,
        "threshold": 0.0,
        "exit_lookback": 21,
        "regime_sma": 200,
        "atr_period": 14,
        "stop_atr": 3.0,
        "target_r": 100.0,
        "allow_short": True,
        "max_holding_bars": 126,
    },
    "orb": {
        "or_bars": 2,
        "atr_period": 14,
        "stop_atr": 0.5,
        "target_r": 1.5,
        "allow_short": True,
        "max_holding_bars": 40,
    },
    "regimeoverlay": {
        "sma_period": 200,
        "atr_period": 14,
        "stop_atr": 3.5,
        "entry_buffer_atr": 0.0,
        "exit_buffer_atr": 0.5,
        "sma_slope_window": 0,
        "allow_short": False,
        "target_r": 100.0,
        "max_holding_bars": 0,
    },
}


class Strategy:
    name = "base"

    def __init__(self, config: RunConfig) -> None:
        self.config = config
        merged = dict(DEFAULTS.get(config.strategy, {}))
        merged.update(config.params or {})
        self.params = merged
        self.max_holding_bars = int(merged.get("max_holding_bars", 0) or 0)
        self._session_start = _clock(config.session_start)
        self._flatten_at = _clock(config.flatten_at)
        self._entry_cutoff = _clock(config.entry_cutoff)

    # -- interface --------------------------------------------------------
    @property
    def warmup(self) -> int:
        return 60

    @property
    def history_depth(self) -> int:
        """Bars of history the strategy needs to evaluate one signal."""
        return self.warmup + 40

    def signal(self, bars: list[Bar]) -> Signal | None:
        raise NotImplementedError

    def exit_signal(self, position: Position, bars: list[Bar]) -> bool:
        return False

    def should_flatten(self, bar: Bar) -> bool:
        if not self.config.intraday_only:
            return False
        return session_time(bar) >= self._flatten_at

    # -- helpers ----------------------------------------------------------
    def _entry_window(self, bar: Bar) -> bool:
        moment = session_time(bar)
        if moment < self._session_start or moment > self._entry_cutoff:
            return False
        return True

    def _atr(self, bars: list[Bar]) -> float | None:
        values = atr(bars, int(self.params["atr_period"]))
        value = values[-1] if values else None
        return float(value) if value and value > 0 else None


class DonchianTrend(Strategy):
    """Channel breakout with a long-term regime filter and channel-based exit."""

    name = "donchian"

    @property
    def warmup(self) -> int:
        return max(int(self.params["entry"]), int(self.params["regime_sma"]), int(self.params["atr_period"])) + 5

    def signal(self, bars: list[Bar]) -> Signal | None:
        if len(bars) < self.warmup:
            return None
        current = bars[-1]
        if self.config.intraday_only and not self._entry_window(current):
            return None
        entry = int(self.params["entry"])
        if len(bars) < entry + 2:
            return None
        window = bars[-entry - 1 : -1]
        upper = max(bar.high for bar in window)
        lower = min(bar.low for bar in window)
        current_atr = self._atr(bars)
        if current_atr is None:
            return None
        closes = [bar.close for bar in bars]
        regime = int(self.params["regime_sma"])
        regime_value = None
        if regime > 0 and len(closes) >= regime:
            regime_value = sma(closes, regime)[-1]
        long_ok = current.close > upper and (regime_value is None or current.close > regime_value)
        short_ok = bool(self.params["allow_short"]) and current.close < lower and (regime_value is None or current.close < regime_value)
        if not long_ok and not short_ok:
            return None
        side = Side.LONG if long_ok else Side.SHORT
        stop_distance = float(self.params["stop_atr"]) * current_atr
        metadata = {"channel_upper": round(upper, 4), "channel_lower": round(lower, 4)}
        if regime_value is not None:
            metadata["regime_sma"] = round(float(regime_value), 4)
        return Signal(
            timestamp=current.timestamp,
            symbol=current.symbol,
            side=side,
            confidence=0.6,
            reason="donchian_breakout",
            atr=current_atr,
            stop_distance=stop_distance,
            target_r=float(self.params["target_r"]),
            meta=metadata,
        )

    def exit_signal(self, position: Position, bars: list[Bar]) -> bool:
        exit_lookback = int(self.params["exit"])
        if len(bars) < exit_lookback + 2:
            return False
        window = bars[-exit_lookback - 1 : -1]
        close = bars[-1].close
        if position.side is Side.LONG:
            return close < min(bar.low for bar in window)
        return close > max(bar.high for bar in window)


class PullbackDip(Strategy):
    """Buy short-term oversold dips while the index is above its long SMA."""

    name = "pullback"

    @property
    def warmup(self) -> int:
        return max(int(self.params["regime_sma"]), int(self.params["ema_period"]) + 5, int(self.params["atr_period"])) + 5

    def signal(self, bars: list[Bar]) -> Signal | None:
        if len(bars) < self.warmup:
            return None
        current = bars[-1]
        if self.config.intraday_only and not self._entry_window(current):
            return None
        closes = [bar.close for bar in bars]
        current_atr = self._atr(bars)
        if current_atr is None:
            return None
        regime_window = int(self.params["regime_sma"])
        if len(closes) < regime_window:
            return None
        regime_value = sma(closes, regime_window)[-1]
        ema_values = ema(closes, int(self.params["ema_period"]))
        rsi_values = rsi(closes, int(self.params["rsi_period"]))
        ema_value = ema_values[-1]
        rsi_value = rsi_values[-1]
        if regime_value is None or ema_value is None or rsi_value is None:
            return None
        uptrend = current.close > regime_value
        oversold = rsi_value <= float(self.params["rsi_level"])
        stretched = current.close <= ema_value - float(self.params["ema_atr"]) * current_atr
        if not (uptrend and (oversold or stretched)):
            return None
        return Signal(
            timestamp=current.timestamp,
            symbol=current.symbol,
            side=Side.LONG,
            confidence=0.6,
            reason="pullback_dip_long",
            atr=current_atr,
            stop_distance=float(self.params["stop_atr"]) * current_atr,
            target_r=float(self.params["target_r"]),
            meta={
                "rsi": round(float(rsi_value), 3),
                "ema": round(float(ema_value), 4),
                "regime_sma": round(float(regime_value), 4),
            },
        )

    def exit_signal(self, position: Position, bars: list[Bar]) -> bool:
        closes = [bar.close for bar in bars]
        rsi_values = rsi(closes, int(self.params["rsi_period"]))
        rsi_value = rsi_values[-1] if rsi_values else None
        if rsi_value is not None and rsi_value >= float(self.params["rsi_exit"]):
            return True
        ema_values = ema(closes, int(self.params["ema_period"]))
        if ema_values and ema_values[-1] is not None:
            current_atr = self._atr(bars)
            if current_atr is not None and bars[-1].close >= ema_values[-1] + current_atr:
                return True
        return False


class TimeSeriesMomentum(Strategy):
    """Weekly time-series momentum with a long-term regime filter."""

    name = "tsmom"

    @property
    def warmup(self) -> int:
        return max(int(self.params["lookback"]), int(self.params["regime_sma"]), int(self.params["atr_period"])) + 5

    def _is_rebalance(self, bars: list[Bar]) -> bool:
        if len(bars) < 2:
            return False
        current_week = bars[-1].timestamp.isocalendar()[:2]
        previous_week = bars[-2].timestamp.isocalendar()[:2]
        return current_week != previous_week

    def signal(self, bars: list[Bar]) -> Signal | None:
        if len(bars) < self.warmup or not self._is_rebalance(bars):
            return None
        current = bars[-1]
        if self.config.intraday_only and not self._entry_window(current):
            return None
        closes = [bar.close for bar in bars]
        momentum = pct_change(closes, int(self.params["lookback"]))
        current_atr = self._atr(bars)
        if momentum is None or current_atr is None:
            return None
        regime = int(self.params["regime_sma"])
        regime_value = sma(closes, regime)[-1] if regime > 0 and len(closes) >= regime else None
        threshold = float(self.params["threshold"])
        long_ok = momentum > threshold and (regime_value is None or current.close > regime_value)
        short_ok = bool(self.params["allow_short"]) and momentum < -threshold and (regime_value is None or current.close < regime_value)
        if not long_ok and not short_ok:
            return None
        return Signal(
            timestamp=current.timestamp,
            symbol=current.symbol,
            side=Side.LONG if long_ok else Side.SHORT,
            confidence=0.6,
            reason="tsmom_weekly",
            atr=current_atr,
            stop_distance=float(self.params["stop_atr"]) * current_atr,
            target_r=float(self.params["target_r"]),
            meta={"momentum": round(float(momentum), 5)},
        )

    def exit_signal(self, position: Position, bars: list[Bar]) -> bool:
        if not self._is_rebalance(bars):
            return False
        closes = [bar.close for bar in bars]
        momentum = pct_change(closes, int(self.params["exit_lookback"]))
        if momentum is None:
            return False
        regime = int(self.params["regime_sma"])
        regime_value = sma(closes, regime)[-1] if regime > 0 and len(closes) >= regime else None
        close = closes[-1]
        if position.side is Side.LONG:
            return momentum < 0 or (regime_value is not None and close < regime_value)
        return momentum > 0 or (regime_value is not None and close > regime_value)


class OpeningRangeBreakout(Strategy):
    """Intraday breakout of the first N bars of the session."""

    name = "orb"

    @property
    def warmup(self) -> int:
        return max(60, int(self.params["atr_period"]) + int(self.params["or_bars"]) + 5)

    def signal(self, bars: list[Bar]) -> Signal | None:
        if not self.config.intraday_only or len(bars) < self.warmup:
            return None
        current = bars[-1]
        if not self._entry_window(current):
            return None
        or_bars = int(self.params["or_bars"])
        day = current.timestamp.date()
        today = [bar for bar in bars if bar.timestamp.date() == day]
        if len(today) <= or_bars:
            return None
        opening = today[:or_bars]
        or_high = max(bar.high for bar in opening)
        or_low = min(bar.low for bar in opening)
        current_atr = self._atr(bars)
        if current_atr is None:
            return None
        long_ok = current.close > or_high
        short_ok = bool(self.params["allow_short"]) and current.close < or_low
        if not long_ok and not short_ok:
            return None
        if long_ok:
            stop_distance = max(current.close - or_low, float(self.params["stop_atr"]) * current_atr)
            side = Side.LONG
        else:
            stop_distance = max(or_high - current.close, float(self.params["stop_atr"]) * current_atr)
            side = Side.SHORT
        return Signal(
            timestamp=current.timestamp,
            symbol=current.symbol,
            side=side,
            confidence=0.6,
            reason="opening_range_breakout",
            atr=current_atr,
            stop_distance=stop_distance,
            target_r=float(self.params["target_r"]),
            meta={"or_high": round(or_high, 4), "or_low": round(or_low, 4)},
        )


class RegimeOverlay(Strategy):
    """Hold long while price is above a long moving average, flat/below otherwise.

    This is the classic tactical-allocation overlay: it does not try to
    predict, it only participates in the part of the trend that exists.
    """

    name = "regimeoverlay"

    @property
    def warmup(self) -> int:
        return int(self.params["sma_period"]) + int(self.params["atr_period"]) + 5

    def _sma_pair(self, bars: list[Bar]) -> tuple[float | None, float | None]:
        closes = [bar.close for bar in bars]
        period = int(self.params["sma_period"])
        if len(closes) < period + 1:
            return None, None
        current = sma(closes, period)[-1]
        previous = sma(closes[:-1], period)[-1]
        return current, previous

    def signal(self, bars: list[Bar]) -> Signal | None:
        if len(bars) < self.warmup:
            return None
        current = bars[-1]
        if self.config.intraday_only and not self._entry_window(current):
            return None
        current_sma, _ = self._sma_pair(bars)
        current_atr = self._atr(bars)
        if current_sma is None or current_atr is None:
            return None
        slope_window = int(self.params.get("sma_slope_window", 0))
        if slope_window > 0:
            closes = [bar.close for bar in bars]
            if len(closes) < self.params["sma_period"] + slope_window + 1:
                return None
            earlier_sma = sma(closes[:-slope_window], int(self.params["sma_period"]))[-1]
            if earlier_sma is None or current_sma <= earlier_sma:
                return None
        momentum_window = int(self.params.get("momentum_filter", 0))
        if momentum_window > 0:
            closes = [bar.close for bar in bars]
            momentum_value = pct_change(closes, momentum_window)
            if momentum_value is None or momentum_value <= 0:
                return None
        buffer = float(self.params["entry_buffer_atr"]) * current_atr
        close = current.close
        long_ok = close > current_sma + buffer
        short_ok = bool(self.params["allow_short"]) and close < current_sma - buffer
        if not long_ok and not short_ok:
            return None
        return Signal(
            timestamp=current.timestamp,
            symbol=current.symbol,
            side=Side.LONG if long_ok else Side.SHORT,
            confidence=0.6,
            reason="regime_long" if long_ok else "regime_short",
            atr=current_atr,
            stop_distance=float(self.params["stop_atr"]) * current_atr,
            target_r=float(self.params["target_r"]),
            meta={"sma": round(float(current_sma), 4)},
        )

    def exit_signal(self, position: Position, bars: list[Bar]) -> bool:
        current_sma, _ = self._sma_pair(bars)
        current_atr = self._atr(bars)
        if current_sma is None or current_atr is None:
            return False
        buffer = float(self.params["exit_buffer_atr"]) * current_atr
        close = bars[-1].close
        if position.side is Side.LONG:
            return close < current_sma - buffer
        return close > current_sma + buffer


REGISTRY: dict[str, type[Strategy]] = {
    DonchianTrend.name: DonchianTrend,
    PullbackDip.name: PullbackDip,
    TimeSeriesMomentum.name: TimeSeriesMomentum,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    RegimeOverlay.name: RegimeOverlay,
}


def make_strategy(config: RunConfig) -> Strategy:
    if config.strategy not in REGISTRY:
        raise ValueError(f"unknown strategy {config.strategy!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[config.strategy](config)


def _clock(value: str) -> time:
    return time.fromisoformat(value)
