"""Read-only Dhan / Groww historical data access."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .bars import Bar, IST, parse_timestamp, save_bars
from .tokens import authorized_get, dhan_post, TokenError


class DataError(RuntimeError):
    pass


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise DataError("non-numeric price in response") from error


def _pick(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    raise DataError("candle is missing a required field")


def _candle(item: Any, expiry: str | None) -> Bar:
    if isinstance(item, dict):
        stamp = _pick(item, "timestamp", "time", "datetime", "date")
        values = {
            "open": _pick(item, "open", "o"),
            "high": _pick(item, "high", "h"),
            "low": _pick(item, "low", "l"),
            "close": _pick(item, "close", "c"),
            "volume": item.get("volume", item.get("v", 0)),
        }
    elif isinstance(item, (list, tuple)) and len(item) >= 5:
        stamp = item[0]
        values = {"open": item[1], "high": item[2], "low": item[3], "close": item[4], "volume": item[5] if len(item) > 5 else 0}
    else:
        raise DataError("invalid candle row")
    bar = Bar(
        timestamp=_timestamp(stamp),
        symbol="",
        open=_number(values["open"]),
        high=_number(values["high"]),
        low=_number(values["low"]),
        close=_number(values["close"]),
        volume=int(_number(values["volume"])),
        expiry=expiry,
    )
    bar.validate()
    return bar


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc).astimezone(IST)
    return parse_timestamp(str(value))


def _rows(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise DataError("response must be a JSON object")
    status = str(payload.get("status", "success")).upper()
    if status not in {"SUCCESS", "OK", "TRUE"}:
        raise DataError("broker returned an unsuccessful status")
    root = payload.get("payload") if isinstance(payload.get("payload"), (dict, list)) else payload.get("data")
    if isinstance(root, list):
        return root
    if not isinstance(root, dict):
        root = payload
    candles = root.get("candles")
    if isinstance(candles, (list, tuple)):
        return list(candles)
    fields = ("timestamp", "open", "high", "low", "close")
    if all(isinstance(root.get(name), list) for name in fields):
        length = min(len(root[name]) for name in fields)
        volumes = root.get("volume")
        out = []
        for index in range(length):
            row = {name: root[name][index] for name in fields}
            if isinstance(volumes, list) and index < len(volumes):
                row["volume"] = volumes[index]
            out.append(row)
        return out
    raise DataError("response contains no candle rows")


def _parse(response: bytes, symbol: str, expiry: str | None, strict: bool = True) -> list[Bar]:
    try:
        payload = json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DataError("broker returned invalid JSON") from error
    bars: list[Bar] = []
    for item in _rows(payload):
        try:
            candle = _candle(item, expiry)
        except (DataError, ValueError):
            if strict:
                raise
            continue
        bars.append(
            Bar(
                timestamp=candle.timestamp,
                symbol=symbol,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                expiry=candle.expiry,
            )
        )
    bars.sort(key=lambda bar: bar.timestamp)
    deduped: list[Bar] = []
    for bar in bars:
        if deduped and deduped[-1].timestamp == bar.timestamp:
            if strict:
                raise DataError("response contains duplicate timestamps")
            continue
        deduped.append(bar)
    if not deduped:
        raise DataError("response contained no usable candles")
    return deduped


def fetch_dhan_daily(
    security_id: str,
    start_date: str,
    end_date: str,
    exchange_segment: str = "NSE_FNO",
    instrument: str = "FUTIDX",
    expiry: str | None = None,
    output: str | None = None,
    symbol_alias: str | None = None,
) -> list[Bar]:
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "fromDate": start_date,
        "toDate": end_date,
        "oi": False,
    }
    response = dhan_post("https://api.dhan.co/v2/charts/historical", payload)
    bars = _parse(response, symbol_alias or str(security_id), expiry, strict=False)
    if output:
        save_bars(output, bars)
    return bars


def fetch_dhan_intraday(
    security_id: str,
    start_date: str,
    end_date: str,
    interval_minutes: int = 15,
    exchange_segment: str = "NSE_FNO",
    instrument: str = "FUTIDX",
    expiry: str | None = None,
    output: str | None = None,
) -> list[Bar]:
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": instrument,
        "interval": str(interval_minutes),
        "fromDate": start_date,
        "toDate": end_date,
        "oi": False,
    }
    response = dhan_post("https://api.dhan.co/v2/charts/intraday", payload)
    bars = _parse(response, str(security_id), expiry)
    if output:
        save_bars(output, bars)
    return bars


def fetch_groww_intraday(
    groww_symbol: str,
    start_time: str,
    end_time: str,
    interval_minutes: int = 15,
    exchange: str = "NSE",
    segment: str = "FNO",
    expiry: str | None = None,
    output: str | None = None,
) -> list[Bar]:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "exchange": exchange,
            "segment": segment,
            "groww_symbol": groww_symbol,
            "start_time": start_time,
            "end_time": end_time,
            "candle_interval": f"{interval_minutes}minute",
        }
    )
    url = f"https://api.groww.in/v1/historical/candles?{query}"
    response = authorized_get(url, "groww", {"X-API-VERSION": "1.0"})
    bars = _parse(response, groww_symbol, expiry)
    if output:
        save_bars(output, bars)
    return bars


__all__ = ["fetch_dhan_daily", "fetch_dhan_intraday", "fetch_groww_intraday", "DataError", "TokenError"]
