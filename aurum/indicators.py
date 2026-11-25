"""Indicator library used by AURUM strategies (Wilder-style, causal)."""

from __future__ import annotations

import math
from collections.abc import Sequence

from .bars import Bar


def sma(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    rolling = 0.0
    for index, value in enumerate(values):
        rolling += value
        if index >= period:
            rolling -= values[index - period]
        if index >= period - 1:
            result[index] = rolling / period
    return result


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    k = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current += (values[index] - current) * k
        result[index] = current
    return result


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    if not bars:
        return []
    ranges = [bars[0].high - bars[0].low]
    for previous, current in zip(bars, bars[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return ranges


def atr(bars: Sequence[Bar], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) < period:
        return result
    ranges = true_ranges(bars)
    current = sum(ranges[:period]) / period
    result[period - 1] = current
    for index in range(period, len(bars)):
        current = (current * (period - 1) + ranges[index]) / period
        result[index] = current
    return result


def rsi(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_from(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[index - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index - 1]) / period
        result[index] = _rsi_from(avg_gain, avg_loss)
    return result


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def adx(bars: Sequence[Bar], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) < period * 2:
        return result
    ranges = true_ranges(bars)
    plus = [0.0]
    minus = [0.0]
    for previous, current in zip(bars, bars[1:]):
        up = current.high - previous.high
        down = previous.low - current.low
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    sr = sum(ranges[1 : period + 1])
    sp = sum(plus[1 : period + 1])
    sm = sum(minus[1 : period + 1])
    dx: list[float | None] = [None] * len(bars)
    for index in range(period, len(bars)):
        if index > period:
            sr = sr - sr / period + ranges[index]
            sp = sp - sp / period + plus[index]
            sm = sm - sm / period + minus[index]
        if sr <= 0:
            dx[index] = 0.0
            continue
        plus_di = 100.0 * sp / sr
        minus_di = 100.0 * sm / sr
        denominator = plus_di + minus_di
        dx[index] = 0.0 if denominator == 0 else 100.0 * abs(plus_di - minus_di) / denominator
    valid = [value for value in dx if value is not None]
    if len(valid) < period:
        return result
    first = 2 * period - 1
    if first >= len(result):
        return result
    current = sum(valid[:period]) / period
    result[first] = current
    for index in range(first + 1, len(bars)):
        if dx[index] is None:
            continue
        current = (current * (period - 1) + dx[index]) / period
        result[index] = current
    return result


def highest(values: Sequence[float]) -> float | None:
    return max(values) if values else None


def lowest(values: Sequence[float]) -> float | None:
    return min(values) if values else None


def pct_change(values: Sequence[float], lookback: int) -> float | None:
    if len(values) <= lookback or lookback <= 0:
        return None
    previous = values[-lookback - 1]
    if previous <= 0:
        return None
    return values[-1] / previous - 1.0


def realized_vol(closes: Sequence[float], window: int) -> float | None:
    if len(closes) <= window:
        return None
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252.0)
