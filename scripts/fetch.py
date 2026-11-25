#!/usr/bin/env python3
"""Fetch historical data from Dhan (read-only) into data/."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.dhan import fetch_dhan_daily, fetch_dhan_intraday  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-fetch")
    parser.add_argument("mode", choices=["daily", "intraday"])
    parser.add_argument("--security-id", default="48704")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--segment", default="NSE_FNO")
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.mode == "daily":
        bars = fetch_dhan_daily(
            args.security_id,
            args.start,
            args.end,
            exchange_segment=args.segment,
            instrument=args.instrument or "FUTIDX",
            output=args.output,
        )
    else:
        bars = fetch_dhan_intraday(
            args.security_id,
            args.start,
            args.end,
            interval_minutes=args.interval,
            exchange_segment=args.segment,
            instrument=args.instrument or "FUTIDX",
            output=args.output,
        )
    print(f"{args.mode}: {len(bars)} bars -> {args.output}")
    print(f"range: {bars[0].timestamp.isoformat()} .. {bars[-1].timestamp.isoformat()}")


if __name__ == "__main__":
    main()
