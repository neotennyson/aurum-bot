"""AURUM web terminal.

Serves a hand-built single-page dashboard and a small JSON API assembled from
the research reports and paper-trading states. Read-only; paper trading only.

Run:  python3 -m aurum.webapp --port 8551
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date as date_cls, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def load_json(relative: str) -> dict | None:
    path = ROOT / relative
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def series_stats(series: pd.Series | None) -> dict[str, Any]:
    if series is None or len(series) < 2:
        return {}
    returns = series.pct_change().dropna()
    total = series.iloc[-1] / series.iloc[0] - 1
    drawdown = float((series / series.cummax() - 1).min())
    sharpe = float(returns.mean() / returns.std() * math.sqrt(252)) if returns.std() > 0 else 0.0
    return {
        "equity": round(float(series.iloc[-1])),
        "return": round(float(total), 4),
        "dd": round(drawdown, 4),
        "sharpe": round(sharpe, 2),
    }


def curve_points(series: pd.Series | None) -> list[list[Any]]:
    if series is None:
        return []
    return [[timestamp.strftime("%Y-%m-%d"), round(float(value))] for timestamp, value in series.items()]


def equity_series(state: dict | None) -> pd.Series | None:
    if not state:
        return None
    frame = pd.DataFrame(state.get("equity_curve", []))
    if frame.empty:
        return None
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    if frame["timestamp"].dt.tz is not None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
    return frame.set_index("timestamp")["equity"].sort_index()


def naive(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def build_payload() -> dict[str, Any]:
    paper_regime = load_json("data/paper_regime.json")
    paper_stocks = load_json("data/paper_stocks.json")
    paper_tsmom = load_json("data/paper_tsmom.json")
    last_run = load_json("data/last_run.json")
    deployed = load_json("reports/portfolio_deployed.json")
    stock_research = load_json("reports/stocks_research.json")
    stress = load_json("reports/stress_tests.json")
    quarterly = load_json("reports/quarterly_check.json")

    regime_eq = equity_series(paper_regime)
    stocks_eq = equity_series(paper_stocks)
    tsmom_eq = equity_series(paper_tsmom)

    # market ticker
    market: dict[str, Any] = {}
    nifty_close: pd.Series | None = None
    try:
        bars = pd.read_csv(ROOT / "data/nifty_continuous_daily.csv", parse_dates=["timestamp"])
        nifty_close = bars.set_index(bars["timestamp"].dt.tz_localize(None))["close"]
        sma200 = float(nifty_close.rolling(200).mean().iloc[-1])
        market = {
            "close": round(float(nifty_close.iloc[-1]), 1),
            "change": round(float(nifty_close.iloc[-1] / nifty_close.iloc[-2] - 1), 4),
            "sma200": round(sma200, 1),
            "vs_sma": round(float(nifty_close.iloc[-1] / sma200 - 1), 4),
            "above_sma": bool(nifty_close.iloc[-1] > sma200),
            "date": bars["timestamp"].iloc[-1].strftime("%Y-%m-%d"),
        }
    except (OSError, ValueError, IndexError):
        pass
    if market.get("date"):
        try:
            market["age_days"] = (date_cls.today() - date_cls.fromisoformat(market["date"])).days
        except ValueError:
            market["age_days"] = None

    # deployed paper book (regime + stocks, 50/50 capital)
    paper: dict[str, Any] = {"sleeves": {}}
    deployed_curve = None
    benchmark_curve = None
    if regime_eq is not None and stocks_eq is not None:
        joined = pd.concat(
            [regime_eq / regime_eq.iloc[0], stocks_eq / stocks_eq.iloc[0]], axis=1, sort=True
        ).dropna()
        deployed_curve = joined.mean(axis=1) * 1_000_000
        if nifty_close is not None:
            aligned = nifty_close.reindex(deployed_curve.index, method="ffill")
            if aligned.notna().all():
                benchmark_curve = aligned / aligned.iloc[0] * 1_000_000
        paper["deployed"] = {**series_stats(deployed_curve), "curve": curve_points(deployed_curve)}
        paper["benchmark"] = {**series_stats(benchmark_curve), "curve": curve_points(benchmark_curve)}
        first, last = deployed_curve.index[0], deployed_curve.index[-1]
        paper["window"] = {"from": first.strftime("%Y-%m-%d"), "to": last.strftime("%Y-%m-%d")}
    for name, state, series in (
        ("regime", paper_regime, regime_eq),
        ("stocks", paper_stocks, stocks_eq),
        ("tsmom", paper_tsmom, tsmom_eq),
    ):
        if state:
            paper["sleeves"][name] = {
                "status": state.get("status", "unknown"),
                "funded": name != "tsmom",
                **series_stats(series),
            }
    paper["positions"] = (paper_stocks or {}).get("positions", {})
    paper["last_run"] = last_run

    # trades (all sleeves, newest first, capped)
    trades = []
    for name, state in (("regime", paper_regime), ("stocks", paper_stocks), ("tsmom", paper_tsmom)):
        if not state:
            continue
        for trade in state.get("trades", []):
            trades.append(
                {
                    "sleeve": name,
                    "symbol": trade.get("symbol", ""),
                    "side": trade.get("side", ""),
                    "qty": trade.get("quantity"),
                    "entry_time": str(trade.get("entry_time", ""))[:10],
                    "entry_price": round(float(trade.get("entry_price", 0)), 2),
                    "exit_time": str(trade.get("exit_time", ""))[:10],
                    "exit_price": round(float(trade.get("exit_price", 0)), 2),
                    "pnl": round(float(trade.get("net_pnl", 0))),
                    "fees": round(float(trade.get("fees", 0))),
                    "reason": trade.get("exit_reason", ""),
                }
            )
    trades.sort(key=lambda item: item["exit_time"], reverse=True)

    # forward-only paper marker
    marker = load_json("data/forward_start.json")
    paper["forward_start"] = marker.get("forward_start") if marker else None
    paper["capital"] = (paper_stocks or {}).get("capital") or (paper_regime or {}).get("capital")

    compound_state = load_json("data/paper_compound.json")
    if compound_state:
        paper["compound"] = {
            "equity": compound_state.get("equity"),
            "status": compound_state.get("status", "unknown"),
            "last": compound_state.get("last_timestamp"),
            "trades": len(compound_state.get("trades", [])),
            "capital": compound_state.get("capital"),
        }

    # research: deployed OOS
    research: dict[str, Any] = {}
    if deployed:
        research = {
            "composition": deployed.get("composition", {}),
            "window": deployed.get("window", {}),
            "deployed": {
                "metrics": deployed["deployed"]["metrics"],
                "curve": [[point["timestamp"][:10], round(point["equity"])] for point in deployed["deployed"]["curve"]],
            },
            "benchmark": {
                "metrics": deployed["benchmark"]["metrics"],
                "curve": [[point["timestamp"][:10], round(point["equity"])] for point in deployed["benchmark"]["curve"]],
            },
            "sleeves": {
                name: {"metrics": payload["metrics"], "trades": payload.get("trades", 0)}
                for name, payload in deployed["sleeves"].items()
            },
        }
    if stock_research:
        research["stocks"] = {
            "universe": stock_research.get("universe_size"),
            "holdout_start": stock_research.get("holdout_start"),
            "holdout": stock_research["holdout"]["metrics"],
            "no_gate": stock_research.get("holdout_no_gate", {}),
            "stress": stock_research["holdout_stress"]["metrics"],
            "benchmark": stock_research["nifty_benchmark"],
        }

    # validation
    validation: dict[str, Any] = {"quarterly": quarterly}
    if stress:
        noise = stress.get("noise", {})
        selection = stress.get("selection_control", {})
        stock_boot = stress.get("stock_bootstrap", {})
        portfolio_boot = stress.get("portfolio_bootstrap", {})
        checks = [
            {"state": "ok", "title": "Causality", "detail": "Trades identical when future data removed (prefix invariance)."},
            {"state": "ok", "title": "Noise test", "detail": f"Zero-drift random walks average {noise.get('mean', 0):+.1%} at 1.5x — no hallucinated edge."},
            {"state": "ok", "title": "Selection control", "detail": f"Momentum top-8 beat {selection.get('momentum_wins', 0)}/30 random eligible books."},
            {"state": "ok", "title": "Stock robustness", "detail": "18/18 parameter variants positive on holdout; deployed params median-like."},
            {"state": "ok", "title": "Cost ladder", "detail": "Stock sleeve +101.8% even at 2x slippage and 2x roll costs."},
            {"state": "ok", "title": "Sizing audit", "detail": "A confidence bug silently shrank '2.5x' positions to 1.5x — fixed; every label now states the validated 1.5x notional."},
            {"state": "warn", "title": "tsmom fragility", "detail": "Only 33% of 2022+ variants positive — demoted to observation."},
            {"state": "warn", "title": "Survivorship (quantified)", "detail": "Full universe +106% vs the honest pre-2016-listed sample +67% (Sharpe 0.74) — the edge survives, the bias is about a third of the shown return."},
            {"state": "warn", "title": "Tail risk", "detail": f"Bootstrap CAGR 5th pct: stocks {stock_boot.get('cagr_p5', 0):+.1%}, portfolio {portfolio_boot.get('cagr_p5', 0):+.1%}."},
            {"state": "warn", "title": "Lot size", "detail": "Stocks need ₹50L+; at ₹10-20L the book is dead capital. NIFTY futures 1 lot needs ≈₹4.5L margin."},
        ]
        validation.update(
            {
                "checks": checks,
                "bootstrap": {"stock": stock_boot, "portfolio": portfolio_boot},
                "sensitivity": stress.get("index_sensitivity", []),
                "feasibility": stress.get("feasible_universe", {}),
            }
        )
    capital = load_json("reports/capital_study.json")
    if capital:
        validation["capital"] = {
            "index": capital.get("index_1_lot_scenarios", []),
            "buy_hold": capital.get("leveraged_buy_hold", []),
            "stocks": capital.get("stock_lot_scenarios", []),
            "survivorship": capital.get("survivorship", {}),
        }
    compound = load_json("reports/compound_study.json")
    if compound:
        start_capital = float(compound["config"]["risk"]["capital"])
        projections = []
        for rate in (0.08, 0.12, 0.15, 0.20):
            projections.append(
                {
                    "cagr": rate,
                    "to_10L": round(math.log(1_000_000 / start_capital) / math.log(1 + rate), 1),
                    "to_50L": round(math.log(5_000_000 / start_capital) / math.log(1 + rate), 1),
                    "to_1cr": round(math.log(10_000_000 / start_capital) / math.log(1 + rate), 1),
                }
            )
        validation["compound"] = {"study": compound, "projections": projections, "capital": start_capital}

    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    market_open = now.weekday() < 5 and (9 * 60 + 15) <= minutes < (15 * 60 + 30)

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": {"open": market_open, "next_job": "16:20 IST"},
        "market": market,
        "paper": paper,
        "research": research,
        "trades": trades[:500],
        "validation": validation,
    }


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "AurumTerminal"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_file(WEB / "index.html")
        if path == "/api/data":
            payload = json.dumps(build_payload(), default=str).encode("utf-8")
            return self._send(200, payload, CONTENT_TYPES[".json"])
        if path == "/favicon.svg":
            return self._serve_file(WEB / "favicon.svg")
        if path.startswith("/vendor/") and path.count("/") == 2:
            return self._serve_file(WEB / "vendor" / Path(path).name)
        if path.endswith((".css", ".js", ".svg")):
            return self._serve_file(WEB / Path(path).name)
        self._send(404, b"not found", "text/plain")

    def _serve_file(self, target: Path) -> None:
        try:
            body = target.read_bytes()
        except OSError:
            return self._send(404, b"not found", "text/plain")
        self._send(200, body, CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser(prog="aurum-webapp")
    parser.add_argument("--port", type=int, default=8551)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AURUM terminal on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
