/* AURUM terminal — hand-built UI. Paper trading only, no order path anywhere. */

const charts = {};
const chartRange = { "chart-overview": "ALL", "chart-performance": "ALL" };
let DATA = null;
let tradeFilter = "all";
let tradeSearch = "";
let visibleTrades = 100;

/* ------------------------------------------------------------- formatting */
const nfIN = new Intl.NumberFormat("en-IN");
const inr = (v) => (v == null ? "—" : "₹" + nfIN.format(Math.round(v)));
const inrC = (v) => {
  if (v == null) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  if (a >= 1e7) return s + "₹" + (a / 1e7).toFixed(2) + "Cr";
  if (a >= 1e5) return s + "₹" + (a / 1e5).toFixed(2) + "L";
  if (a >= 1e3) return s + "₹" + (a / 1e3).toFixed(1) + "k";
  return s + "₹" + Math.round(a);
};
const pct = (v, d = 1) => (v == null ? "—" : (v * 100).toFixed(d) + "%");
const spct = (v, d = 1) => (v == null ? "—" : (v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%");
const num = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "muted");
const priceStr = (v) => (v == null ? "—" : Number(v).toFixed(1));

function spark(values, color, w = 76, h = 24) {
  if (!values || values.length < 3) return "";
  const v = values.slice(-72);
  const min = Math.min(...v), max = Math.max(...v), range = max - min || 1;
  const points = v.map((x, i) => [
    ((i / (v.length - 1)) * (w - 2) + 1).toFixed(1),
    (h - 2 - ((x - min) / range) * (h - 6)).toFixed(1),
  ]);
  return '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none"><polyline points="' +
    points.map((p) => p.join(",")).join(" ") + '" fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linejoin="round"/></svg>';
}

const kpiCard = (label, value, sub, sparkSvg, delta) =>
  '<div class="card kpi"><div class="top"><div><div class="klabel">' + label + '</div><div class="value">' + value + "</div>" +
  '<div class="sub">' + (sub || "&nbsp;") + "</div></div><div>" + (delta || "") + sparkSvg + "</div></div></div>";

const rowsCard = (title, rows, hint) =>
  '<div class="card"><h3>' + title + (hint ? '<span class="hint">' + hint + "</span>" : "") +
  '</h3><div class="rows">' +
  rows.map((r) => '<div class="row"><span class="k">' + r[0] + '</span><span class="v">' + r[1] + (r[2] ? '<span class="sub">' + r[2] + "</span>" : "") + "</span></div>").join("") +
  "</div></div>";

const rangeBar = (chartId) =>
  '<span class="ranges" data-chart="' + chartId + '">' +
  ["1M", "3M", "1Y", "ALL"].map((r) =>
    '<button data-range="' + r + '"' + (chartRange[chartId] === r ? ' class="active"' : "") + ">" + r + "</button>").join("") +
  "</span>";

/* ---------------------------------------------------------------- charts */
function sliceCurve(curve, range) {
  if (!curve || range === "ALL") return curve || [];
  const days = { "1M": 31, "3M": 92, "1Y": 366 }[range] || 0;
  const last = new Date(curve[curve.length - 1][0]).getTime();
  const cutoff = last - days * 86400000;
  const sliced = curve.filter(([t]) => new Date(t).getTime() >= cutoff);
  return sliced.length > 2 ? sliced : curve;
}

function makeChart(id, defs, height = 320) {
  const el = document.getElementById(id);
  if (!el || !window.LightweightCharts) return;
  if (charts[id]) { charts[id].chart.remove(); delete charts[id]; }
  const chart = LightweightCharts.createChart(el, {
    autoSize: true, height,
    layout: { background: { type: "solid", color: "transparent" }, textColor: "#7C8598", fontFamily: '"SF Mono", Menlo, monospace', fontSize: 10.5 },
    localization: { priceFormatter: (p) => nfIN.format(Math.round(p)) },
    watermark: { visible: true, text: "AURUM", fontSize: 46, color: "rgba(233,237,245,0.028)", horzAlign: "center", vertAlign: "center" },
    grid: { vertLines: { color: "#12161F" }, horzLines: { color: "#12161F" } },
    rightPriceScale: { borderColor: "#1F2530", scaleMargins: { top: 0.09, bottom: 0.09 } },
    timeScale: { borderColor: "#1F2530" },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Magnet,
      vertLine: { color: "#2A3140", labelBackgroundColor: "#1A2030" },
      horzLine: { color: "#2A3140", labelBackgroundColor: "#1A2030" },
    },
    handleScroll: false, handleScale: false,
  });
  const series = [];
  for (const def of defs) {
    let s;
    if (def.type === "area") {
      s = chart.addAreaSeries({ lineColor: def.color, topColor: def.color + "26", bottomColor: def.color + "00", lineWidth: 2, priceLineVisible: false });
    } else {
      s = chart.addLineSeries({ color: def.color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false });
    }
    s.setData(def.data.map(([t, v]) => ({ time: t, value: v })));
    series.push(s);
  }
  charts[id] = { chart, series, defs };
  if (chartRange[id] && chartRange[id] !== "ALL") applyRange(id, chartRange[id]);
  else chart.timeScale().fitContent();
}

function applyRange(id, range) {
  chartRange[id] = range;
  const entry = charts[id];
  if (!entry) return;
  entry.series.forEach((s, i) => {
    s.setData(sliceCurve(entry.defs[i].data, range).map(([t, v]) => ({ time: t, value: v })));
  });
  entry.chart.timeScale().fitContent();
}

function activateCharts(page) {
  if (!DATA) return;
  if (page === "overview" && DATA.paper.deployed) {
    const defs = [{ type: "area", color: "#F0B429", data: DATA.paper.deployed.curve }];
    if (DATA.paper.benchmark && DATA.paper.benchmark.curve.length) defs.push({ type: "line", color: "#5B6472", data: DATA.paper.benchmark.curve });
    makeChart("chart-overview", defs, 330);
  }
  if (page === "performance" && DATA.research.deployed) {
    makeChart("chart-performance", [
      { type: "area", color: "#F0B429", data: DATA.research.deployed.curve },
      { type: "line", color: "#5B6472", data: DATA.research.benchmark.curve },
    ], 330);
  }
}

/* ---------------------------------------------------------------- header */
function renderTicker(data) {
  const tk = document.getElementById("ticker");
  const m = data.market || {};
  const bits = [];
  if (m.close != null) {
    bits.push('<span class="tk"><span class="lbl">NIFTY</span><b>' + nfIN.format(m.close) + "</b><span class=\"" + cls(m.change) + "\">" + spct(m.change) + "</span></span>");
    bits.push('<span class="tk"><span class="lbl">200-SMA</span><b>' + nfIN.format(m.sma200) + "</b></span>");
    bits.push('<span class="tk"><span class="lbl">vs SMA</span><span class="' + cls(m.vs_sma) + '">' + spct(m.vs_sma) + "</span></span>");
    bits.push('<span class="chip ' + (m.above_sma ? "ok" : "warn") + '">Market gate ' + (m.above_sma ? "long" : "cash") + "</span>");
  } else {
    bits.push('<span class="fine">market data unavailable</span>');
  }
  if (m.age_days != null && m.age_days >= 2) {
    bits.push('<span class="chip bad">data stale ' + m.age_days + "d</span>");
  }
  tk.innerHTML = bits.join("");
  const status = data.market_status || {};
  const live = status.open
    ? '<span class="live">market open</span>'
    : '<span class="live closed">market closed</span>';
  document.getElementById("meta").innerHTML =
    live + " · next job " + (status.next_job || "—") + "<br/>data " + (m.date || "—") + " · terminal " + (data.generated_at || "—") + " IST";
}

/* --------------------------------------------------------------- pages */
function overviewPage(data) {
  const p = data.paper || {};
  const q = (data.validation || {}).quarterly;
  const quarterly = q ? rowsCard("Quarterly review", [
    ["Regime", '<span class="gold">' + q.regime.status + "</span>", "median variant " + spct(q.regime.median_return)],
    ["tsmom", '<span class="gold">' + q.tsmom.status + "</span>", "median " + spct(q.tsmom.median_return)],
    ["Stocks", '<span class="pos">' + spct(q.stocks.return) + "</span>", "sharpe " + num(q.stocks.sharpe)],
  ], q.as_of) : "";

  const sleeveRows = [];
  for (const [name, s] of Object.entries(p.sleeves || {})) {
    const label = name + (s.funded ? "" : " · obs");
    sleeveRows.push([label, (s.status === "flat" ? '<span class="chip plain">flat</span>' : '<span class="chip ok">invested</span>'),
      inr(s.equity != null ? s.equity : p.capital)]);
  }
  const posture = rowsCard("Book status", sleeveRows);

  if (!p.deployed) {
    const comp = p.compound || {};
    const compStudy = (data.validation || {}).compound || {};
    let compoundCard = "";
    if (comp && compStudy.study) {
      const s = compStudy.study;
      const proj = (compStudy.projections || []).map((r) =>
        "<tr><td>" + Math.round(r.cagr * 100) + "%</td><td class='r'>" + r.to_10L + "y</td><td class='r'>" + r.to_50L + "y</td><td class='r'>" + r.to_1cr + "y</td></tr>").join("");
      compoundCard =
        '<div class="card" style="margin-top:14px"><h3>Compound account · real NIFTY lots <span class="hint">margin-scaled, adds lots as equity grows</span></h3>' +
        '<div class="chips" style="margin-bottom:10px">' +
        '<span class="chip plain">' + (comp.status || "flat") + "</span>" +
        '<span class="chip info">capital ' + inrC(comp.capital) + "</span>" +
        '<span class="chip warn">replay 2016→26 ' + spct(s.full.total_return) + " · dd " + pct(s.full.max_drawdown) + "</span>" +
        '<span class="chip bad">replay 2022+ ' + spct(s.holdout_2022_plus.total_return) + "</span></div>" +
        '<div class="fine-copy">One NIFTY lot while equity covers its margin (' + inrC(s.margin_per_lot_today) + " per lot today); the account adds lots automatically as it compounds (replay used up to " + s.max_lots_used + "). " +
        "Honest: at 1-lot leverage the chop years are brutal, so the book stays in cash during bad regimes and waits for the trend. Years to reach a target at a given CAGR:</div>" +
        '<table class="tbl" style="margin-top:8px"><thead><tr><th>cagr</th><th class="r">₹10L</th><th class="r">₹50L</th><th class="r">₹1Cr</th></tr></thead><tbody>' + proj + "</tbody></table></div>";
    }
    return '<div class="card"><h3>Forward paper book <span class="hint">no replays — evidence only from here</span></h3><div class="fine-copy">' +
      'Started <b>' + (p.forward_start || "—") + "</b> with " + inrC(p.capital || 1000000) + " per sleeve intact. <b>Zero trades so far</b> — every trade from here is genuine " +
      "out-of-sample paper trading recorded in real time. The 2024→2026 replay history (105 trades) is archived under <b>data/archive</b> and appears nowhere as paper results." +
      '<div class="chips" style="margin-top:12px"><span class="chip info">next job 16:20 IST</span><span class="chip ok">paper only</span></div></div></div>' +
      '<div class="grid two" style="margin-top:14px">' + posture +
      '<div>' + rowsCard("Open positions", [["Regime book", '<span class="chip plain">flat</span>'], ["Stock book", '<span class="chip plain">cash · gate off</span>']], "no open risk today") + "</div></div>" +
      compoundCard +
      (quarterly ? '<div class="grid" style="margin-top:14px">' + quarterly + "</div>" : "");
  }
  const curve = p.deployed.curve.map(([, v]) => v);
  const bm = p.benchmark || {};
  const kpis = [
    kpiCard("Deployed equity", inrC(p.deployed.equity), "regime + stocks · 50/50", spark(curve.slice(-90), "#38BDF8"), ""),
    kpiCard("Return", spct(p.deployed.return), "since " + (p.window ? p.window.from : ""), "", '<span class="delta ' + (p.deployed.return >= 0 ? "pos" : "neg") + '">NIFTY ' + spct(bm.return) + "</span>"),
    kpiCard("Max drawdown", pct(p.deployed.dd), "paper history", "", ""),
    kpiCard("Sharpe", num(p.deployed.sharpe), "daily, annualised", "", ""),
  ].join("");

  const holdings = p.positions || {};
  const holdingRows = Object.entries(holdings).slice(0, 10);
  const positions = holdingRows.length
    ? '<div class="card"><h3>Open positions<span class="hint">paper</span></h3><div class="rows">' +
      holdingRows.map(([symbol, entry]) =>
        '<div class="row"><span class="sym">' + symbol + '</span><span class="v">' + priceStr(entry.entry_price) + ' <span class="muted">× ' + entry.quantity + '</span><span class="sub">since ' + String(entry.entry_time).slice(0, 10) + "</span></span></div>").join("") + "</div></div>"
    : rowsCard("Open positions", [["Regime book", '<span class="chip plain">flat</span>'], ["Stock book", '<span class="chip plain">cash</span>'], ["Next action", '<span class="muted">weekly rebalance check</span>']], "no open risk today");

  return '<div class="grid kpis">' + kpis + "</div>" +
    '<div class="grid two" style="margin-top:14px">' +
    '<div class="card"><h3>Deployed equity <span class="hint">amber aurum · gray nifty</span>' + rangeBar("chart-overview") + '</h3><div id="chart-overview" class="chart"></div></div>' +
    '<div style="display:flex;flex-direction:column;gap:14px">' + posture + positions + "</div></div>" +
    (quarterly ? '<div class="grid" style="margin-top:14px">' + quarterly + "</div>" : "");
}

function performancePage(data) {
  const r = data.research || {};
  if (!r.deployed) return '<div class="empty">No research report — run scripts/portfolio_deployed.py</div>';
  const m = r.deployed.metrics, b = r.benchmark.metrics;
  const kpis = [
    kpiCard("OOS return", spct(m.total_return), "7 rolling years", "", ""),
    kpiCard("CAGR", pct(m.cagr), "vs NIFTY " + pct(b.cagr), "", ""),
    kpiCard("Max drawdown", pct(m.max_drawdown), "vs NIFTY " + pct(b.max_drawdown), "", ""),
    kpiCard("Sharpe", num(m.sharpe), "vs NIFTY " + num(b.sharpe), "", ""),
  ].join("");

  const sleeveRows = Object.entries(r.sleeves || {}).map(([name, s]) => {
    const mm = s.metrics;
    const role = name === "tsmom_observation" ? '<span class="tag tsmom">observation</span>' : '<span class="tag stocks">funded</span>';
    return "<tr><td>" + name.replace("_observation", "") + " " + role + "</td><td class='r " + cls(mm.total_return) + "'>" + spct(mm.total_return) + "</td><td class='r'>" + pct(mm.cagr) + "</td><td class='r'>" + pct(mm.max_drawdown) + "</td><td class='r'>" + num(mm.sharpe) + "</td><td class='r'>" + (s.trades || 0) + "</td></tr>";
  }).join("");

  let stocks = "";
  if (r.stocks) {
    const h = r.stocks.holdout, g = r.stocks.no_gate, s = r.stocks.stress;
    stocks = '<div class="card" style="margin-top:14px"><h3>Stock sleeve · ' + r.stocks.universe + ' NSE F&amp;O names <span class="hint">weekly momentum · market gate</span></h3>' +
      '<div class="chips" style="margin-bottom:12px">' +
      '<span class="chip ok">holdout ' + spct(h.total_return) + "</span>" +
      '<span class="chip info">sharpe ' + num(h.sharpe) + "</span>" +
      '<span class="chip warn">dd ' + pct(h.max_drawdown) + " (no gate " + pct(g.max_drawdown) + ")</span>" +
      '<span class="chip plain">cost stress ' + spct(s.total_return) + "</span>" +
      "</div>" +
      '<div class="fine-copy">Gate: invests only while NIFTY trades above its 200-day average — cut the covid drawdown from −40% to −14%. Real futures lots need roughly ₹5M+ capital to execute; at ₹10M results are essentially unchanged.</div></div>';
  }

  return '<div class="grid kpis">' + kpis + "</div>" +
    '<div class="card" style="margin-top:14px"><h3>Out-of-sample portfolio <span class="hint">' + r.window.first_test + " → " + r.window.last_test + "</span>" + rangeBar("chart-performance") + '</h3><div id="chart-performance" class="chart"></div></div>' +
    '<div class="card" style="margin-top:14px"><h3>Sleeve contributions</h3><table class="tbl"><thead><tr><th>sleeve</th><th class="r">return</th><th class="r">cagr</th><th class="r">max dd</th><th class="r">sharpe</th><th class="r">trades</th></tr></thead><tbody>' + sleeveRows + "</tbody></table></div>" +
    stocks;
}

function tradesPage(data) {
  const trades = data.trades || [];
  if (!trades.length) return '<div class="empty">No forward trades yet — the 105 historical replay trades are archived (data/archive) and are not counted as paper results.</div>';
  const net = trades.reduce((a, t) => a + t.pnl, 0);
  const fees = trades.reduce((a, t) => a + t.fees, 0);
  const wins = trades.filter((t) => t.pnl > 0).length;
  const kpis = [
    kpiCard("Trades", String(trades.length), "all sleeves", "", ""),
    kpiCard("Net P&L", inrC(net), "paper, all-time", "", ""),
    kpiCard("Win rate", Math.round((wins / trades.length) * 100) + "%", wins + " winners", "", ""),
    kpiCard("Fees", inrC(fees), "brokerage · STT · GST", "", ""),
  ].join("");
  return '<div class="grid kpis">' + kpis + "</div>" +
    '<div class="filters" style="margin-top:16px" id="trade-filters">' +
    ["all", "regime", "stocks", "tsmom"].map((f) =>
      '<button class="filter' + (tradeFilter === f ? " active" : "") + '" data-filter="' + f + '">' + f + "</button>").join("") +
    '<input class="search" id="trade-search" placeholder="search symbol…" value="' + tradeSearch.replace(/"/g, "&quot;") + '"/>' +
    '<button class="filter" id="dl-trades" style="margin-left:auto">Download CSV</button></div>' +
    '<div class="table-wrap"><table class="tbl" id="trades-table"></table></div>' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px">' +
    '<span class="fine" id="trade-hint"></span><button class="filter" id="more-trades" style="display:none">Show more</button></div>';
}

function filteredTrades() {
  const query = tradeSearch.trim().toUpperCase();
  return (DATA.trades || []).filter((t) =>
    (tradeFilter === "all" || t.sleeve === tradeFilter) &&
    (!query || String(t.symbol).toUpperCase().includes(query)));
}

function renderTradeRows() {
  const table = document.getElementById("trades-table");
  if (!table) return;
  const all = filteredTrades();
  const rows = all.slice(0, visibleTrades);
  const head = "<thead><tr><th>exit</th><th>sleeve</th><th>symbol</th><th>side</th><th class='r'>qty</th><th class='r'>entry</th><th class='r'>exit</th><th class='r'>net p&amp;l</th><th class='r'>fees</th><th>reason</th></tr></thead>";
  const body = rows.map((t) =>
    "<tr><td class='muted'>" + t.exit_time + "</td><td><span class='tag " + t.sleeve + "'>" + t.sleeve + "</span></td><td>" + t.symbol + "</td>" +
    "<td><span class='tag " + String(t.side).toLowerCase() + "'>" + t.side + "</span></td><td class='r'>" + t.qty + "</td>" +
    "<td class='r'>" + priceStr(t.entry_price) + "</td><td class='r'>" + priceStr(t.exit_price) + "</td>" +
    "<td class='r " + cls(t.pnl) + "'>" + (t.pnl >= 0 ? "+" : "") + nfIN.format(t.pnl) + "</td>" +
    "<td class='r muted'>" + nfIN.format(t.fees) + "</td><td class='muted'>" + t.reason + "</td></tr>").join("");
  table.innerHTML = head + "<tbody>" + body + "</tbody>";
  const hint = document.getElementById("trade-hint");
  if (hint) hint.textContent = "showing " + rows.length + " of " + all.length + " trades";
  const more = document.getElementById("more-trades");
  if (more) {
    more.style.display = all.length > rows.length ? "inline-block" : "none";
    more.onclick = () => { visibleTrades += 100; renderTradeRows(); };
  }
}

function bindTrades() {
  if (!document.getElementById("trades-table")) return;
  renderTradeRows();
  document.querySelectorAll("#trade-filters .filter[data-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      tradeFilter = button.dataset.filter;
      visibleTrades = 100;
      document.querySelectorAll("#trade-filters .filter[data-filter]").forEach((b) => b.classList.toggle("active", b === button));
      renderTradeRows();
    });
  });
  const search = document.getElementById("trade-search");
  if (search) search.addEventListener("input", () => { tradeSearch = search.value; visibleTrades = 100; renderTradeRows(); });
  const dl = document.getElementById("dl-trades");
  if (dl) dl.addEventListener("click", () => {
    const header = "sleeve,symbol,side,qty,entry_time,entry_price,exit_time,exit_price,net_pnl,fees,reason";
    const lines = (DATA.trades || []).map((t) => [t.sleeve, t.symbol, t.side, t.qty, t.entry_time, t.entry_price, t.exit_time, t.exit_price, t.pnl, t.fees, t.reason].join(","));
    const blob = new Blob([header + "\n" + lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "aurum_trades.csv";
    a.click();
  });
}

function validationPage(data) {
  const v = data.validation || {};
  const checks = (v.checks || []).map((c) =>
    '<div class="check ' + c.state + '"><span class="bar"></span><div class="body"><div class="title">' + c.title + '</div><div class="detail">' + c.detail + "</div></div></div>").join("");
  const q = v.quarterly;
  const qcard = q ? '<div class="card"><h3>Quarterly review <span class="hint">' + q.as_of + '</span></h3><div class="rows">' +
    '<div class="row"><span class="k">Regime</span><span class="v"><span class="gold">' + q.regime.status + '</span><span class="sub">median ' + spct(q.regime.median_return) + " · " + Math.round(q.regime.positive_share * 100) + "% variants +</span></span></div>" +
    '<div class="row"><span class="k">tsmom</span><span class="v"><span class="gold">' + q.tsmom.status + '</span><span class="sub">median ' + spct(q.tsmom.median_return) + " · " + Math.round(q.tsmom.positive_share * 100) + "% variants +</span></span></div>" +
    '<div class="row"><span class="k">Stocks</span><span class="v"><span class="pos">' + spct(q.stocks.return) + '</span><span class="sub">sharpe ' + num(q.stocks.sharpe) + "</span></span></div></div></div>" : "";

  const boot = v.bootstrap || {};
  const bootRows = [];
  if (boot.stock && boot.stock.cagr_p5 != null) {
    bootRows.push(["Stock CAGR p5 / p50", spct(boot.stock.cagr_p5) + " / " + spct(boot.stock.cagr_p50)]);
    bootRows.push(["Stock DD p5", pct(boot.stock.dd_p5)]);
    bootRows.push(["Stock P(profit)", Math.round(boot.stock.prob_positive * 100) + "%"]);
  }
  if (boot.portfolio && boot.portfolio.cagr_p5 != null) {
    bootRows.push(["Portfolio CAGR p5 / p50", spct(boot.portfolio.cagr_p5) + " / " + spct(boot.portfolio.cagr_p50)]);
    bootRows.push(["Portfolio DD p5", pct(boot.portfolio.dd_p5)]);
    bootRows.push(["Portfolio P(profit)", Math.round(boot.portfolio.prob_positive * 100) + "%"]);
  }
  const bootCard = bootRows.length ? rowsCard("Bootstrap · 20-day blocks, 2000 runs", bootRows, "bad-luck scenarios") : "";

  const feas = Object.entries(v.feasibility || {}).map(([capital, f]) =>
    "<tr><td>₹" + (capital / 1e6).toFixed(1) + "M</td><td class='r'>" + f.feasible_names + "</td><td>" + (f.executable ? "<span class='chip ok'>yes</span>" : "<span class='chip bad'>no</span>") + "</td><td class='r'>" + (f.return != null ? spct(f.return) : "—") + "</td></tr>").join("");
  const feasCard = feas ? '<div class="card"><h3>Stock futures · lot-size feasibility</h3><table class="tbl"><thead><tr><th>capital</th><th class="r">feasible names</th><th>executable</th><th class="r">holdout</th></tr></thead><tbody>' + feas + "</tbody></table></div>" : "";

  const cap = v.capital || {};
  let capitalCard = "";
  if ((cap.index || []).length) {
    const shown = cap.index.filter((r) => r.capital >= 300000);
    const indexRows = shown.map((r) =>
      "<tr><td>₹" + (r.capital / 1e5).toFixed(1) + "L" + (r.margin_cap >= 0.9 ? "" : " · 60%") + "</td><td>" + r.family + "</td>" +
      "<td class='r'>" + (r.leverage_at_entry ? r.leverage_at_entry.toFixed(1) + "x" : "—") + "</td>" +
      "<td class='r " + cls(r.full_return) + "'>" + spct(r.full_return) + "</td>" +
      "<td class='r'>" + pct(r.full_dd) + "</td><td class='r'>" + r.full_trades + "</td></tr>").join("");
    const wiped = (cap.buy_hold || []).filter((r) => !r.survives).length;
    const stockRows = (cap.stocks || []).map((r) =>
      "<tr><td>₹" + (r.capital / 1e5).toFixed(1) + "L</td><td class='r'>" + (r.trades || 0) + "</td><td class='r'>" + spct(r.holdout_return) + "</td><td class='r'>" + pct(r.holdout_dd) + "</td></tr>").join("");
    capitalCard =
      '<div class="card" style="margin-top:14px"><h3>Small-capital reality · 1–5L <span class="hint">real 1-lot sizing, margin included</span></h3>' +
      '<div class="chips" style="margin-bottom:10px"><span class="chip bad">leveraged buy&amp;hold: margin call in ' + wiped + "/4 cases</span>" +
      '<span class="chip ok">AURUM sleeves survive via stops</span></div>' +
      '<table class="tbl"><thead><tr><th>capital</th><th>sleeve</th><th class="r">lev</th><th class="r">2016→26</th><th class="r">max dd</th><th class="r">trades</th></tr></thead><tbody>' + indexRows + "</tbody></table>" +
      '<div class="fine-copy" style="margin-top:10px">At ₹2L even one NIFTY lot is out of margin range. At ₹3–5L one lot trades at 2.1–1.3x leverage and survived every crisis on record — while a plain leveraged buy&amp;hold of the same size gets wiped out by the −38.8% covid move. Stock sleeve with real lots below ₹50L is dead capital:</div>' +
      '<table class="tbl" style="margin-top:8px"><thead><tr><th>capital</th><th class="r">trades</th><th class="r">holdout</th><th class="r">dd</th></tr></thead><tbody>' + stockRows + "</tbody></table></div>";
  }

  const sens = (v.sensitivity || []);
  let sensCard = "";
  if (sens.length) {
    const grouped = {};
    for (const item of sens) {
      const g = grouped[item.strategy] || { n: 0, best: -9, med: [], worst: 9 };
      g.n += 1; g.best = Math.max(g.best, item.return); g.worst = Math.min(g.worst, item.return); g.med.push(item.return);
      grouped[item.strategy] = g;
    }
    const rows = Object.entries(grouped).map(([name, g]) => {
      g.med.sort((a, b) => a - b);
      const med = g.med[Math.floor(g.med.length / 2)];
      const state = name === "regime_daily" ? "ok" : "warn";
      return "<tr><td>" + name.replace("_daily", "") + " <span class='chip " + state + " plain'>" + (state === "ok" ? "robust" : "fragile") + "</span></td><td class='r'>" + g.n + "</td><td class='r'>" + spct(g.best) + "</td><td class='r " + cls(med) + "'>" + spct(med) + "</td><td class='r'>" + spct(g.worst) + "</td></tr>";
    }).join("");
    sensCard = '<div class="card"><h3>Index sensitivity · 2022+ variants</h3><table class="tbl"><thead><tr><th>strategy</th><th class="r">variants</th><th class="r">best</th><th class="r">median</th><th class="r">worst</th></tr></thead><tbody>' + rows + "</tbody></table></div>";
  }

  const compStudy = v.compound || {};
  let compoundMathCard = "";
  if (compStudy.study) {
    const proj = (compStudy.projections || []).map((r) =>
      "<tr><td>" + Math.round(r.cagr * 100) + "%</td><td class='r'>" + r.to_10L + "y</td><td class='r'>" + r.to_50L + "y</td><td class='r'>" + r.to_1cr + "y</td></tr>").join("");
    compoundMathCard =
      '<div class="card"><h3>Compounding math <span class="hint">from ' + inrC(compStudy.capital) + '</span></h3>' +
      '<div class="fine-copy">At the replay CAGR of 14%, the account reaches ₹10L in ~6 years, ₹50L in ~18, ₹1Cr in ~24 — <b>if</b> it never hits a drawdown year. A −50% drawdown resets the clock by ~5 years, which is exactly why the book sits in cash during bad regimes instead of fighting them.</div>' +
      '<table class="tbl" style="margin-top:8px"><thead><tr><th>cagr</th><th class="r">₹10L</th><th class="r">₹50L</th><th class="r">₹1Cr</th></tr></thead><tbody>' + proj + "</tbody></table></div>";
  }

  return '<div class="grid two"><div class="card"><h3>Report card <span class="hint">trying to break the system, not sell it</span></h3><div class="checks">' + checks + "</div></div>" +
    '<div style="display:flex;flex-direction:column;gap:14px">' + qcard + bootCard + compoundMathCard + "</div></div>" +
    '<div class="grid halves" style="margin-top:14px">' + sensCard + feasCard + "</div>" +
    capitalCard +
    '<div class="card" style="margin-top:14px"><h3>How AURUM decides</h3><div class="fine-copy">' +
    "<b>Regime sleeve</b> (funded, NIFTY 2.5x): long while price holds above the 200-day average, flat below SMA − 0.5×ATR. " +
    "<b>Stock sleeve</b> (funded, 1x): weekly top-8 6-month momentum above their own 200-SMA; invests only while NIFTY itself is above its 200-SMA. " +
    "<b>tsmom</b>: observation only until it passes the quarterly robustness gate. " +
    "<b>Risk:</b> ATR stops, daily-loss breaker, cooldowns, long-only — shorting breakdowns failed testing. " +
    "<b>Costs:</b> ₹20/order cap plus exchange, STT, stamp, SEBI and GST, with slippage. " +
    "<b>Honest limits:</b> survivorship bias in the stock universe, overnight gap risk, circuit-limit risk in panics, and no guarantee of future profit. Paper money only.</div></div>";
}

/* ------------------------------------------------------------ rendering */
function bindRanges() {
  document.querySelectorAll(".ranges").forEach((group) => {
    group.querySelectorAll("button").forEach((button) => {
      button.classList.toggle("active", chartRange[group.dataset.chart] === button.dataset.range);
      button.addEventListener("click", () => {
        applyRange(group.dataset.chart, button.dataset.range);
        group.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === button));
      });
    });
  });
}

function render(data) {
  DATA = data;
  renderTicker(data);
  document.getElementById("page-overview").innerHTML = overviewPage(data);
  document.getElementById("page-performance").innerHTML = performancePage(data);
  document.getElementById("page-trades").innerHTML = tradesPage(data);
  document.getElementById("page-validation").innerHTML = validationPage(data);
  bindTrades();
  bindRanges();
  activateCharts(currentPage);
}

function showPage(page) {
  currentPage = page;
  document.querySelectorAll(".nav").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  document.querySelectorAll(".page").forEach((section) => section.classList.toggle("active", section.id === "page-" + page));
  if (location.hash.slice(1) !== page) history.replaceState(null, "", "#" + page);
  activateCharts(page);
}

let currentPage = (location.hash || "#overview").slice(1);
if (!document.getElementById("page-" + currentPage)) currentPage = "overview";

document.querySelectorAll(".nav").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));

async function load() {
  try {
    const response = await fetch("/api/data", { cache: "no-store" });
    render(await response.json());
  } catch (error) {
    document.getElementById("ticker").innerHTML = '<span class="chip bad">connection lost</span>';
  }
}

document.addEventListener("DOMContentLoaded", () => {
  showPage(currentPage);
  load();
  setInterval(() => { if (!document.hidden) load(); }, 300000);
});
