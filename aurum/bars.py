"""OHLCV bar model, CSV persistence and session helpers."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    expiry: str | None = None

    def validate(self) -> None:
        prices = (self.open, self.high, self.low, self.close)
        if any(not math.isfinite(value) for value in prices):
            raise ValueError("bar prices must be finite")
        if any(value <= 0 for value in prices):
            raise ValueError("bar prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("bar high/low inconsistent with open/close")
        if self.low > self.high or self.volume < 0:
            raise ValueError("bar range or volume invalid")


def parse_timestamp(value: str | int | float) -> datetime:
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value), tz=ZoneInfo("UTC"))
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("timestamp is empty")
        if text.replace(".", "", 1).isdigit():
            moment = datetime.fromtimestamp(float(text), tz=ZoneInfo("UTC"))
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def load_bars(path: str | Path, symbol: str | None = None) -> list[Bar]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(str(source))
    bars: list[Bar] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row")
        for number, row in enumerate(reader, start=2):
            stamp = row.get("timestamp") or row.get("datetime")
            if not stamp:
                stamp = f"{row.get('date', '')} {row.get('time', '')}".strip()
            row_symbol = (row.get("symbol") or symbol or "NIFTY").strip()
            if symbol is not None and row_symbol != symbol:
                continue
            try:
                bar = Bar(
                    timestamp=parse_timestamp(stamp),
                    symbol=row_symbol,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row.get("volume") or 0)),
                    expiry=(row.get("expiry") or None),
                )
                bar.validate()
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid row {number}: {error}") from error
            bars.append(bar)
    if not bars:
        raise ValueError("no bars matched the requested symbol")
    bars.sort(key=lambda item: item.timestamp)
    for earlier, later in zip(bars, bars[1:]):
        if earlier.timestamp == later.timestamp:
            raise ValueError("duplicate timestamp in bar data")
    return bars


def save_bars(path: str | Path, bars: Iterable[Bar]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = ["timestamp", "symbol", "open", "high", "low", "close", "volume", "expiry"]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "timestamp": bar.timestamp.isoformat(),
                    "symbol": bar.symbol,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "expiry": bar.expiry or "",
                }
            )


def session_time(bar: Bar) -> time:
    return bar.timestamp.timetz().replace(tzinfo=None)


def is_regular_session(bar: Bar) -> bool:
    return time(9, 15) <= session_time(bar) <= time(15, 30)


def median_interval_minutes(bars: list[Bar]) -> int:
    if len(bars) < 2:
        return 0
    diffs = [
        (later.timestamp - earlier.timestamp).total_seconds() / 60.0
        for earlier, later in zip(bars, bars[1:])
        if later.timestamp.date() == earlier.timestamp.date()
    ]
    if not diffs:
        return 0
    return int(round(sorted(diffs)[len(diffs) // 2]))


def trading_days(bars: list[Bar]) -> list[date]:
    seen: list[date] = []
    for bar in bars:
        day = bar.timestamp.date()
        if not seen or seen[-1] != day:
            seen.append(day)
    return seen
