#!/usr/bin/env python3
"""Build the liquid NSE stock-F&O universe and fetch continuous daily history.

Reads the Dhan scrip master, keeps the nearest live expiry per stock future,
resolves its security id, and downloads its continuous daily candles.
Resumable: symbols with an existing file are skipped.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import load_bars  # noqa: E402
from aurum.dhan import fetch_dhan_daily  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "data/reference/dhan_scrip_master.csv"
OUTPUT_DIR = ROOT / "data/stocks"


def build_universe(master_path: Path) -> pd.DataFrame:
    frame = pd.read_csv(master_path, dtype=str)
    nse = frame[(frame["SEM_EXM_EXCH_ID"] == "NSE") & (frame["SEM_INSTRUMENT_NAME"] == "FUTSTK")].copy()
    nse["symbol"] = nse["SEM_TRADING_SYMBOL"].str.split("-").str[0]
    nse["expiry"] = pd.to_datetime(nse["SEM_EXPIRY_DATE"]).dt.date
    nse = nse[~nse["symbol"].str.contains("NSETEST", case=False, na=False)]
    nse = nse[nse["symbol"].str.fullmatch(r"[A-Z][A-Z0-9&\-]*")]
    today = date.today()
    nse = nse[nse["expiry"] >= today]
    nearest = nse.sort_values("expiry").groupby("symbol", as_index=False).first()
    nearest["lot_units"] = nearest["SEM_LOT_UNITS"].astype(float).astype(int)
    return nearest[["symbol", "SEM_SMST_SECURITY_ID", "lot_units", "expiry"]].rename(
        columns={"SEM_SMST_SECURITY_ID": "security_id"}
    ).sort_values("symbol").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-fetch-stocks")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=1.1)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    universe = build_universe(MASTER)
    universe.to_csv(ROOT / "data/reference/fno_universe.csv", index=False)
    print(f"universe: {len(universe)} stock futures with a live expiry")
    if args.limit:
        universe = universe.head(args.limit)

    fetched = skipped = failed = 0
    for row in universe.itertuples(index=False):
        destination = OUTPUT_DIR / f"{row.symbol}.csv"
        if destination.exists():
            try:
                if len(load_bars(destination, row.security_id)) > 500:
                    skipped += 1
                    continue
            except ValueError:
                pass
        try:
            bars = fetch_dhan_daily(
                row.security_id,
                args.start,
                args.end,
                exchange_segment="NSE_FNO",
                instrument="FUTSTK",
                output=str(destination),
            )
            fetched += 1
            print(f"[{fetched}] {row.symbol:<14} {len(bars):>5} bars  {bars[0].timestamp.date()} -> {bars[-1].timestamp.date()}", flush=True)
        except Exception as error:  # noqa: BLE001 - keep the batch going
            failed += 1
            print(f"FAIL {row.symbol}: {type(error).__name__}: {error}", flush=True)
        time.sleep(args.sleep)
    print(f"done: fetched={fetched} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
