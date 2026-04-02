"""
Web dashboard — professional terminal-style UI served on port 8080.
Matches the look of the reference screenshot:
  • Header bar with mode, uptime, cycle, divergence
  • Big stat blocks: Balance, Total P&L, Scan Latency, Win Rate
  • Info strip: BTC price, CLOB mid, fair, edge, orders, daily P&L
  • Left panel: live equity curve (Chart.js)
  • Right panel: scrolling execution log
  • Download button: exports full training data as JSON

Runs in a daemon thread.  Access via http://<nas-ip>:8080
Auto-refreshes every 2 seconds.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from flask import Flask, jsonify, Response

import config

if TYPE_CHECKING:
    from src.dashboard import DashboardState

logging.getLogger("werkzeug").setLevel(logging.ERROR)

app  = Flask(__name__)
_state: DashboardState | None = None

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return HTML

@app.route("/api/state")
def api_state():
    if _state is None:
        return jsonify({"status": "starting"})
    return jsonify(_build(  _state))

@app.route("/api/export")
def api_export():
    """Download full training data as a JSON file for analysis."""
    from pathlib import Path
    journal_path = Path("data/trade_journal.json")
    params_path  = Path("data/learned_params.json")

    journal = []
    if journal_path.exists():
        with open(journal_path) as f:
            journal = json.load(f)

    params = {}
    if params_path.exists():
        with open(params_path) as f:
            params = json.load(f)

    payload = {
        "exported_at":    datetime.utcnow().isoformat() + "Z",
        "config": {
            "dry_run":              config.DRY_RUN,
            "max_position_usdc":    config.MAX_POSITION_USDC,
            "max_total_exposure":   config.MAX_TOTAL_EXPOSURE_USDC,
            "kelly_fraction":       config.KELLY_FRACTION,
            "min_edge":             config.MIN_EDGE,
        },
        "performance": {
            "total_trades": _state.total_trades if _state else 0,
            "wins":         _state.wins         if _state else 0,
            "total_pnl":    _state.total_pnl    if _state else 0,
            "total_fees":   _state.total_fees   if _state else 0,
            "win_rate":     _state.win_rate      if _state else 0,
            "best_trade":   _state.best_trade    if _state else 0,
            "worst_trade":  _state.worst_trade   if _state else 0,
        },
        "learned_params":  params,
        "trade_journal":   journal,
        "equity_curve":    _state.equity_curve if _state else [],
    }

    blob = json.dumps(payload, indent=2)
    return Response(
        blob,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=midusbot_training_data.json"},
    )

# ---------------------------------------------------------------------------
# State serialiser
# ---------------------------------------------------------------------------

def _build(s: DashboardState) -> dict:
    return {
        "mode":            "DRY-RUN" if config.DRY_RUN else "LIVE",
        "uptime":          s.uptime,
        "loop_count":      s.loop_count,
        "markets_scanned": s.markets_scanned,
        "candidates":      s.candidates,
        "exposure":        s.exposure,
        "max_exposure":    config.MAX_TOTAL_EXPOSURE_USDC,
        "gas_cost_usdc":   config.GAS_COST_USDC,
        "maker_fee_pct":   config.MAKER_FEE_PCT,
        "daily_pnl":       s.daily_pnl,
        "balance":         s.balance,
        "wallet_balance":  s.wallet_balance,
        "seed":            s._seed,
        "scan_latency_ms": s.scan_latency_ms,
        "orders_placed":   s.orders_placed,
        "btc_price":       s.btc_price,
        "equity_curve":    s.equity_curve[-300:],   # last 300 points for chart
        "exec_log":        s.exec_log[:80],
        "positions": [
            {
                "question":      pos.question[:55],
                "side":          pos.side,
                "shares":        pos.shares,
                "entry_price":   pos.entry_price,
                "current_price": cur,
                "pnl_usdc":      round(pos.shares * (cur - pos.entry_price), 4),
                "pnl_pct":       (cur - pos.entry_price) / pos.entry_price if pos.entry_price else 0,
            }
            for pos, cur in s.positions
        ],
        "signals": [
            {
                "confidence":     sig.confidence,
                "side":           sig.side,
                "question":       sig.question[:55],
                "market_price":   sig.market_price,
                "fair_value":     sig.fair_value,
                "edge":           sig.edge,
                "signal":         sig.signal,
                "is_latency_arb": sig.is_latency_arb,
            }
            for sig in s.recent_signals
        ],
        "performance": {
            "total_pnl":    s.total_pnl,
            "total_fees":   s.total_fees,
            "win_rate":     s.win_rate,
            "total_trades": s.total_trades,
            "wins":         s.wins,
            "best_trade":   s.best_trade,
            "worst_trade":  s.worst_trade,
        },
        "learned": s.learned,
    }

# ---------------------------------------------------------------------------
# Start helper
# ---------------------------------------------------------------------------

def start(state: DashboardState, port: int = 8080) -> None:
    global _state
    _state = state
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
        name="webui",
    )
    t.start()

# ---------------------------------------------------------------------------
# HTML — single file, no templates
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MIDUSBOT</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root{
  --bg:#080c10;--bg2:#0d1219;--bg3:#111820;--bg4:#161e28;
  --border:#1c2a38;--text:#b0c8e0;--dim:#4a6070;
  --green:#00e676;--red:#ff1744;--yellow:#ffab00;
  --blue:#40c4ff;--purple:#e040fb;--cyan:#00e5ff;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace;font-size:12px;display:flex;flex-direction:column}

/* ── HEADER ── */
#hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;height:40px;min-height:40px;
  background:var(--bg2);border-bottom:1px solid var(--border);
}
#hdr-left{display:flex;align-items:center;gap:12px}
#bot-name{color:var(--blue);font-size:14px;letter-spacing:3px;font-weight:bold}
.mode-badge{padding:2px 10px;border-radius:12px;font-size:10px;font-weight:bold}
.dry {background:#001a0a;color:var(--green);border:1px solid var(--green)}
.live{background:#1a0005;color:var(--red);  border:1px solid var(--red)}
#hdr-right{display:flex;gap:20px;color:var(--dim);font-size:11px}
#hdr-right span{color:var(--text)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;margin-right:6px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}

/* ── STAT BLOCKS ── */
#stats{
  display:grid;grid-template-columns:repeat(5,1fr);
  border-bottom:1px solid var(--border);
}
.stat-block{padding:12px 20px;border-right:1px solid var(--border)}
.stat-block:last-child{border-right:none}
.stat-lbl{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.stat-val{font-size:28px;font-weight:bold;line-height:1}
.stat-sub{color:var(--dim);font-size:11px;margin-top:3px}

/* ── INFO STRIP ── */
#info{
  display:flex;gap:24px;flex-wrap:wrap;
  padding:6px 20px;background:var(--bg2);
  border-bottom:1px solid var(--border);
  font-size:11px;color:var(--dim);
}
#info span{color:var(--text)}
#info .sep{color:var(--border)}

/* ── MAIN AREA ── */
#main{flex:1;display:grid;grid-template-columns:1fr 340px;overflow:hidden;min-height:0}

/* ── CHART PANEL ── */
#chart-panel{
  display:flex;flex-direction:column;
  border-right:1px solid var(--border);
  padding:12px 16px;overflow:hidden;
}
#chart-title{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;display:flex;justify-content:space-between}
#chart-title span{color:var(--cyan)}
#chart-wrap{flex:1;position:relative;min-height:0}

/* ── EXEC LOG ── */
#log-panel{display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}
#log-title{
  padding:10px 14px 8px;color:var(--dim);
  font-size:10px;text-transform:uppercase;letter-spacing:1px;
  border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;
}
#log-body{flex:1;overflow-y:auto;padding:6px 0}
#log-body::-webkit-scrollbar{width:4px}
#log-body::-webkit-scrollbar-track{background:transparent}
#log-body::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.log-row{padding:3px 14px;line-height:1.5;font-size:11px;border-left:2px solid transparent}
.log-row:hover{background:var(--bg3)}
.log-ts{color:var(--dim);margin-right:6px;font-size:10px}
.k-divergence{color:var(--cyan); border-left-color:var(--cyan)!important}
.k-exec      {color:var(--text); border-left-color:var(--border)!important}
.k-filled    {color:var(--green);border-left-color:var(--green)!important}
.k-slipped   {color:var(--red);  border-left-color:var(--red)!important}
.k-scan      {color:#2a4060;     border-left-color:transparent!important}
.k-info      {color:var(--dim);  border-left-color:transparent!important}
.k-arb       {color:var(--purple);border-left-color:var(--purple)!important}

/* ── FOOTER ── */
#footer{
  display:flex;align-items:center;justify-content:space-between;
  padding:5px 20px;background:var(--bg2);border-top:1px solid var(--border);
  font-size:10px;color:var(--dim);min-height:28px;
}
#footer a{
  color:var(--blue);text-decoration:none;
  border:1px solid var(--blue);padding:2px 12px;border-radius:3px;
  font-size:10px;letter-spacing:1px;
}
#footer a:hover{background:var(--blue);color:var(--bg)}

/* ── COLOURS ── */
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.b{color:var(--blue)}.p{color:var(--purple)}.d{color:var(--dim)}
.tag-arb{font-size:9px;background:#1a0a2a;color:var(--purple);border-radius:2px;padding:1px 4px;margin-left:4px;vertical-align:middle}
</style>
</head>
<body>

<!-- HEADER -->
<div id="hdr">
  <div id="hdr-left">
    <span id="bot-name">MIDUSBOT</span>
    <span id="ver" style="color:var(--dim);font-size:11px">// PYTHON ENGINE v1.0.0</span>
    <span class="dot"></span>
    <span id="mode-badge" class="mode-badge dry">DRY-RUN</span>
  </div>
  <div id="hdr-right">
    <div>Uptime <span id="h-uptime">00:00:00</span></div>
    <div>Cycle <span id="h-cycle">#0</span></div>
    <div>Edge <span id="h-edge" class="g">—</span></div>
    <div>Markets <span id="h-markets">—</span></div>
    <div id="h-refresh" style="color:var(--dim)">connecting…</div>
  </div>
</div>

<!-- STAT BLOCKS -->
<div id="stats">
  <div class="stat-block">
    <div class="stat-lbl">Balance</div>
    <div class="stat-val b" id="s-balance">$0.00</div>
    <div class="stat-sub" id="s-bal-sub">seed: <span id="s-seed" class="d">$0</span></div>
  </div>
  <div class="stat-block">
    <div class="stat-lbl">Total P&amp;L</div>
    <div class="stat-val g" id="s-pnl">+$0.00</div>
    <div class="stat-sub" id="s-pnl-pct">+0.0%</div>
  </div>
  <div class="stat-block">
    <div class="stat-lbl">Scan Latency</div>
    <div class="stat-val" id="s-latency">0ms</div>
    <div class="stat-sub d">feed → order</div>
  </div>
  <div class="stat-block">
    <div class="stat-lbl">Win Rate</div>
    <div class="stat-val" id="s-winrate">0%</div>
    <div class="stat-sub" id="s-winsub">0W / 0T</div>
  </div>
  <div class="stat-block">
    <div class="stat-lbl">Fees Paid</div>
    <div class="stat-val r" id="s-fees">$0.00</div>
    <div class="stat-sub d">gas + maker fee</div>
  </div>
</div>

<!-- INFO STRIP -->
<div id="info">
  <div>BTC/USD <span id="i-btc" class="b">—</span></div>
  <div class="sep">|</div>
  <div>CLOB mid <span id="i-mid">—</span></div>
  <div>fair <span id="i-fair" class="g">—</span></div>
  <div>edge <span id="i-edge" class="g">—</span></div>
  <div class="sep">|</div>
  <div>orders/<span id="i-orders">0</span></div>
  <div>daily P&amp;L <span id="i-daily">$0.00</span></div>
  <div>risk used <span id="i-risk">0%</span></div>
  <div class="sep">|</div>
  <div>positions <span id="i-pos">0</span></div>
  <div class="sep">|</div>
  <div>fees/trade <span id="i-fee-per" class="r">$0.04</span></div>
  <div>total fees <span id="i-fees-total" class="r">$0.00</span></div>
</div>

<!-- MAIN -->
<div id="main">

  <!-- EQUITY CURVE -->
  <div id="chart-panel">
    <div id="chart-title">
      <span>EQUITY CURVE // PORTFOLIO VALUE</span>
      <span id="chart-now">$0.00</span>
    </div>
    <div id="chart-wrap">
      <canvas id="eq-chart"></canvas>
    </div>
  </div>

  <!-- EXECUTION LOG -->
  <div id="log-panel">
    <div id="log-title">
      <span>EXECUTION LOG</span>
      <span id="log-count" class="d">0 entries</span>
    </div>
    <div id="log-body"></div>
  </div>

</div>

<!-- FOOTER -->
<div id="footer">
  <div id="f-refresh">last update: <span id="f-time">—</span></div>
  <div style="display:flex;gap:12px;align-items:center">
    <span id="f-learn" class="d">0 adaptations</span>
    <a href="/api/export" download="midusbot_training_data.json">⬇ EXPORT TRAINING DATA</a>
  </div>
</div>

<script>
// ── CHART SETUP ──────────────────────────────────────────────────────────
const ctx = document.getElementById('eq-chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    datasets: [{
      data: [],
      borderColor: '#40c4ff',
      borderWidth: 1.5,
      backgroundColor: 'rgba(64,196,255,0.06)',
      fill: true,
      pointRadius: 0,
      tension: 0.2,
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { intersect: false, mode: 'index' },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: ctx => '$' + ctx.parsed.y.toFixed(2)
        },
        backgroundColor: '#0d1219',
        borderColor: '#1c2a38',
        borderWidth: 1,
        titleColor: '#4a6070',
        bodyColor: '#b0c8e0',
      }
    },
    scales: {
      x: {
        type: 'time',
        time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } },
        grid:  { color: '#0f1a24' },
        ticks: { color: '#2a4060', maxTicksLimit: 6, font: { size: 10 } },
        border:{ color: '#1c2a38' }
      },
      y: {
        grid:  { color: '#0f1a24' },
        ticks: {
          color: '#2a4060',
          font: { size: 10 },
          callback: v => '$' + v.toFixed(0)
        },
        border:{ color: '#1c2a38' }
      }
    }
  }
});

// ── HELPERS ──────────────────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const cc = n  => n > 0 ? 'g' : n < 0 ? 'r' : 'd';
function setC(id, text, cls) {
  const el = $(id); if (!el) return;
  el.textContent = text;
  if (cls !== undefined) el.className = cls;
}
function fmtS(sec) {
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = Math.floor(sec%60);
  return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
}
let _startTs = null;

// ── MAIN REFRESH ─────────────────────────────────────────────────────────
async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    if (d.status === 'starting') { $('h-refresh').textContent = 'starting…'; return; }

    if (!_startTs) _startTs = Date.now();

    // Header
    const badge = $('mode-badge');
    badge.textContent = d.mode;
    badge.className   = 'mode-badge ' + (d.mode==='LIVE' ? 'live' : 'dry');
    $('h-uptime').textContent  = d.uptime;
    $('h-cycle').textContent   = '#' + d.loop_count;
    $('h-markets').textContent = d.candidates + '/' + d.markets_scanned;
    $('h-refresh').textContent = 'updated ' + new Date().toLocaleTimeString();

    // Best edge from recent signals
    if (d.signals && d.signals.length) {
      const best = Math.max(...d.signals.map(s=>s.edge));
      $('h-edge').textContent = '+' + (best*100).toFixed(2) + '%';
    }

    // Stat blocks
    const pnl    = d.performance.total_pnl;
    const fees   = d.performance.total_fees || 0;
    const pnlPct = d.seed > 0 ? pnl / d.seed * 100 : 0;
    if (d.wallet_balance > 0) {
      setC('s-balance', '$' + d.wallet_balance.toFixed(2), 'stat-val b');
      $('s-bal-sub').innerHTML = 'wallet · P&L <span class="'+cc(pnl)+'">'+(pnl>=0?'+':'')+pnl.toFixed(2)+'</span>';
    } else {
      setC('s-balance', '$' + d.balance.toFixed(2), 'stat-val b');
      setC('s-seed',    '$' + d.seed.toFixed(0));
    }
    setC('s-pnl',     (pnl>=0?'+':'')+' $'+Math.abs(pnl).toFixed(2), 'stat-val '+cc(pnl));
    $('s-pnl-pct').innerHTML = `<span class="${cc(pnl)}">${pnl>=0?'+':''}${pnlPct.toFixed(1)}%</span>`;
    setC('s-latency', d.scan_latency_ms + 'ms');
    const wr = d.performance.win_rate;
    setC('s-winrate', (wr*100).toFixed(1)+'%', 'stat-val '+(d.performance.total_trades>5?(wr>=.5?'g':'r'):''));
    setC('s-winsub',  d.performance.wins+'W / '+d.performance.total_trades+'T');
    setC('s-fees',    '-$' + fees.toFixed(4), 'stat-val r');

    // Info strip
    setC('i-btc',    d.btc_price > 0 ? '$'+d.btc_price.toLocaleString(undefined,{maximumFractionDigits:0}) : '—');
    if (d.signals && d.signals.length) {
      const s = d.signals[0];
      setC('i-mid',  s.market_price.toFixed(3));
      setC('i-fair', s.fair_value.toFixed(3));
      setC('i-edge', '+'+(s.edge*100).toFixed(2)+'%', 'g');
    }
    setC('i-orders', d.orders_placed);
    const dp = d.daily_pnl;
    setC('i-daily', (dp>=0?'+':'')+' $'+Math.abs(dp).toFixed(2), cc(dp));
    const riskPct = d.max_exposure>0 ? (d.exposure/d.max_exposure*100).toFixed(0) : 0;
    setC('i-risk',  riskPct+'%', riskPct>=90?'r':riskPct>=60?'y':'g');
    setC('i-pos',   d.positions.length);

    // Fees (2 gas txs per round-trip + maker fee on typical position size)
    const typicalPos = d.max_exposure * 0.005;  // ~0.5% of exposure cap
    const feePerTrade = 2 * (d.gas_cost_usdc||0.02) + typicalPos * (d.maker_fee_pct||0) * 2;
    setC('i-fee-per',    '$' + feePerTrade.toFixed(3));
    setC('i-fees-total', '-$' + fees.toFixed(4));

    // Equity curve
    if (d.equity_curve && d.equity_curve.length) {
      chart.data.datasets[0].data = d.equity_curve.map(p => ({ x: p.t, y: p.v }));
      chart.update('none');
      const last = d.equity_curve[d.equity_curve.length-1].v;
      $('chart-now').textContent = '$'+last.toFixed(2);
      $('chart-now').className   = cc(last - d.seed);
    }

    // Exec log
    const logBody = $('log-body');
    if (d.exec_log && d.exec_log.length) {
      $('log-count').textContent = d.exec_log.length + ' entries';
      logBody.innerHTML = d.exec_log.map(e =>
        `<div class="log-row k-${e.kind}">` +
        `<span class="log-ts">${e.ts}</span>${escHtml(e.text)}` +
        `</div>`
      ).join('');
    }

    // Footer
    $('f-time').textContent = new Date().toLocaleTimeString();
    if (d.learned) {
      $('f-learn').textContent = (d.learned.adaptation_count||0) + ' adaptations · thresh '+(d.learned.signal_threshold||.15).toFixed(3);
    }

  } catch(e) {
    $('h-refresh').textContent = 'connection error — retrying…';
  }
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""
