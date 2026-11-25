# AURUM — NIFTY F&O research and paper-trading bot

**Paper only.** AURUM contains no order-placement code, no live broker write path and no
credentials beyond read-only historical-data tokens. It cannot trade real money even if
you ask it to.

AURUM's deployed book is two funded long-only sleeves plus one sleeve kept under
observation:

| Sleeve | Role | Logic |
| --- | --- | --- |
| `regime` | **funded**, NIFTY futures 1.5x notional | Long while close > 200-day SMA; flat below SMA − 0.5×ATR |
| `stocks` | **funded**, 192 stock futures 1.0x | Weekly: close > 200-SMA, 126-day return > 0, top 8 momentum, keep inside rank 16, market gate (invests only while NIFTY > 200-SMA) |
| `tsmom` | **observation only** | Weekly: 63-day momentum > 0 and price > 200-SMA — demoted after failing 2022+ parameter-robustness testing |

Capital is split 50/50 between the two funded sleeves. Deployed out-of-sample
(2019→2026 rolling one-year windows): **+111.3%, CAGR 10.8%, max DD −19.5%, Sharpe 0.78**
vs NIFTY +208.9%, DD −38.8%, Sharpe 0.68. The stock sleeve needs roughly ₹5M+ capital to
be executable with real F&O lot sizes (₹10M for the full universe).

## Quickstart

```bash
python3 scripts/fetch.py daily --security-id 48704 --start 2016-01-01 --output data/nifty_continuous_daily.csv
python3 scripts/fetch.py intraday --security-id 48704 --start 2026-07-03 --interval 15 --output data/nifty_oct2026_15m.csv
python3 scripts/research.py --data data/nifty_continuous_daily.csv            # walk-forward family study
python3 scripts/portfolio_research.py                                          # fixed-parameter OOS + scaling
python3 scripts/paper.py replay --data data/nifty_continuous_daily.csv \
    --config config/regime_daily.json --state data/paper_regime.json --start 2024-01-01T00:00:00+05:30
python3 scripts/paper.py replay --data data/nifty_continuous_daily.csv \
    --config config/tsmom_daily.json --state data/paper_tsmom.json --start 2024-01-01T00:00:00+05:30
python3 scripts/fetch_stocks.py                     # build + fetch all liquid stock futures (resumable, ~4 min)
python3 scripts/stocks_research.py                  # stock sleeve backtest, holdout + cost stress
python3 scripts/stocks_paper.py replay --start 2024-01-01
python3 scripts/stocks_paper.py live                # after close: refresh stock data and advance the paper book
./scripts/run_paper_live.sh        # after market close: fetch fresh Dhan bars and advance both index sleeves
python3 -m aurum.webapp --port 8551   # web terminal (custom HTML/CSS/JS + JSON API)
python3 -m unittest discover -s tests
```

## Dashboard and automation

The AURUM terminal is a hand-built single-page app (`web/`) served by a small stdlib
Python API (`aurum/webapp.py`) on its own port (**8551**) — TradingView Lightweight
Charts for the equity curves, dense terminal-style tables, no framework. A Cloudflare
quick tunnel publishes it; the public URL is stored in `AURUM_DASHBOARD_URL.txt` and
rotates automatically if the tunnel drops.

The supervisor daemon keeps everything running and on schedule:

* keeps the dashboard and tunnel alive (restarts them if they die),
* runs the **daily paper job** on weekdays after 16:20 IST,
* runs the **quarterly robustness check** on/after the first day of Jan/Apr/Jul/Oct.

```bash
./scripts/start_aurum.sh    # start or restart everything (also after a reboot)
./scripts/stop_aurum.sh     # stop daemon + dashboard + tunnel
tail -f logs/daemon.log     # supervisor log
tail -f logs/daily_$(date +%F).log   # today's paper job
```

Optional (survives reboots without manual start): launchd plists are kept in `deploy/`.
macOS privacy protection (TCC) blocks scheduled processes from reading `~/Documents`
unless you grant Full Disk Access to `/bin/bash` in System Settings → Privacy & Security,
or move this project outside `~/Documents`. If you do that, copy the plists to
`~/Library/LaunchAgents/` and `launchctl bootstrap gui/$(id -u) <plist>`. The daemon
approach above needs no system permissions.

Named tunnel on your own domain (needs your Cloudflare login once):

```bash
./bin/cloudflared tunnel login                                   # pick example.invalid in the browser
./bin/cloudflared tunnel create aurum
./bin/cloudflared tunnel route dns aurum aurum.example.invalid
./bin/cloudflared tunnel run --url http://localhost:8551 aurum
```

## What the research found (out-of-sample, 7 rolling years through Sep 2026)

| System | Return | CAGR | Max DD | Sharpe |
| --- | --- | --- | --- | --- |
| AURUM portfolio 1.5× notional (regime + tsmom) | +101.5% | 10.0% | −22.1% | 0.73 |
| AURUM portfolio 1.0× | +36.8% | 4.4% | −9.8% | 0.72 |
| Buy & hold NIFTY, same windows | +115.0% | ~11.5% | −38.8% | 0.70 |

Key honest observations:

* AURUM does **not** beat buy-and-hold on raw return. It targets similar or slightly better
  risk-adjusted return with roughly one-third of the drawdown, and every sleeve is
  out-of-sample positive.
* Re-selecting parameters each window underperformed fixing robust parameters
  (regime: +17.6% re-selected vs +37.9% fixed). Deployment uses fixed parameters.
* Shorting NIFTY breakdowns was a consistent loser; both deployed sleeves are long-only.
* Intraday opening-range breakout had no edge on the available 46-session sample and was
  not deployed.

Nothing here guarantees future profit. Paper trade first; that is the whole point.

---

AURUM · Powered by DeepSeek 4.1 Flash
