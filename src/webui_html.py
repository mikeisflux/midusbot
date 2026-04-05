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

/* ── LEARNING BAR ── */
#learn-bar-wrap{
  padding:6px 20px;background:var(--bg2);border-bottom:1px solid var(--border);
  display:none;align-items:center;gap:16px;font-size:11px;
}
#learn-bar-wrap.visible{display:flex}
.lb-label{color:var(--dim);white-space:nowrap}
.lb-track{flex:1;height:6px;background:var(--bg3);border-radius:3px;position:relative;overflow:hidden}
.lb-fill{height:100%;border-radius:3px;transition:width .5s ease}
.lb-fill.red   {background:var(--red)}
.lb-fill.yellow{background:var(--yellow)}
.lb-fill.green {background:var(--green)}
.lb-val{color:var(--text);white-space:nowrap;min-width:36px;text-align:right}
.lb-ready{color:var(--green);font-weight:bold;letter-spacing:1px;animation:pulse 2s infinite}

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

/* ── SIMULATION PANEL ── */
#sim-panel{
  border-top:1px solid var(--border);background:var(--bg2);
  padding:10px 20px 14px;
}
#sim-header{
  display:flex;align-items:center;justify-content:space-between;
  font-size:10px;color:var(--blue);letter-spacing:.08em;font-weight:600;
  text-transform:uppercase;padding-bottom:8px;
}
#sim-stats-row{display:flex;gap:20px;font-size:10px;color:var(--dim)}
.sim-val{color:var(--fg);font-weight:600}
#sim-body{display:grid;grid-template-columns:1fr 1fr;gap:12px;min-height:130px}
#sim-chart-wrap{position:relative;height:130px}
#sim-trades-list{
  font-size:10px;font-family:monospace;overflow-y:auto;max-height:130px;
  display:flex;flex-direction:column;gap:3px;
}
.sim-row{
  display:flex;justify-content:space-between;align-items:center;
  padding:3px 6px;border-radius:2px;background:var(--bg);
  border-left:2px solid var(--border);
}
.sim-row.win{border-left-color:var(--green)}
.sim-row.loss{border-left-color:var(--red)}
.sim-row .sim-q{color:var(--dim);overflow:hidden;white-space:nowrap;text-overflow:ellipsis;max-width:260px}
.sim-row .sim-side{font-weight:700;margin:0 6px;min-width:24px}
.sim-row .sim-p{font-weight:700;min-width:52px;text-align:right}
.sim-row.win .sim-p{color:var(--green)}
.sim-row.loss .sim-p{color:var(--red)}

/* ── FOOTER ── */
#footer{
  display:flex;align-items:center;justify-content:space-between;
  padding:5px 20px;background:var(--bg2);border-top:1px solid var(--border);
  font-size:10px;color:var(--dim);min-height:28px;
}
#footer a, .btn-action{
  color:var(--blue);text-decoration:none;
  border:1px solid var(--blue);padding:2px 12px;border-radius:3px;
  font-size:10px;letter-spacing:1px;cursor:pointer;background:transparent;
  font-family:inherit;
}
#footer a:hover,.btn-action:hover{background:var(--blue);color:var(--bg)}
.btn-danger{color:var(--red)!important;border-color:var(--red)!important}
.btn-danger:hover{background:var(--red)!important;color:var(--bg)!important}
.btn-dim{color:var(--dim)!important;border-color:var(--border)!important}
.btn-dim:hover{background:var(--border)!important;color:var(--text)!important}

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
    <div id="h-clock" style="color:var(--cyan);font-size:11px;letter-spacing:1px;white-space:nowrap">—</div>
    <div>Uptime <span id="h-uptime">00:00:00</span></div>
    <div>Cycle <span id="h-cycle">#0</span></div>
    <div>Edge <span id="h-edge" class="g">—</span></div>
    <div>Markets <span id="h-markets">—</span></div>
    <button id="mode-toggle" class="btn-action" onclick="toggleMode()" style="font-size:10px;letter-spacing:1px">⇄ SANDBOX</button>
    <a href="/positions" class="btn-action" style="font-size:10px;letter-spacing:1px;text-decoration:none">POSITIONS <span id="h-pos-badge" style="background:var(--blue);color:var(--bg);border-radius:8px;padding:1px 6px;font-size:9px">0</span></a>
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
  <div class="stat-block">
    <div class="stat-lbl">Edge Interval</div>
    <div class="stat-val" id="s-edge-avg">—</div>
    <div class="stat-sub">avg t+<span id="s-edge-sub">—</span>s into window</div>
  </div>
</div>

<!-- LEARNING PROGRESS BAR (dry-run only) -->
<div id="learn-bar-wrap">
  <span class="lb-label">DRY-RUN LEARNING</span>
  <span class="lb-label" id="lb-adapt-lbl" style="color:var(--dim)">next adapt</span>
  <div class="lb-track"><div class="lb-fill" id="lb-adapt-fill" style="width:0%"></div></div>
  <span class="lb-val" id="lb-adapt-val">0/10</span>
  <span class="lb-label" style="margin-left:8px">ready to go live</span>
  <div class="lb-track"><div class="lb-fill" id="lb-ready-fill" style="width:0%"></div></div>
  <span class="lb-val" id="lb-ready-val">0%</span>
  <span id="lb-ready-badge" style="display:none" class="lb-ready">✓ READY</span>
</div>

<!-- INFO STRIP -->
<div id="info">
  <div>BTC/USD <span id="i-btc" class="b">—</span></div>
  <div class="sep">|</div>
  <div>next window <span id="i-next-win" class="b">—</span></div>
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
      <div style="display:flex;gap:8px;align-items:center">
        <span id="log-count" class="d">0 entries</span>
        <button class="btn-action btn-dim" onclick="clearLogs()">CLEAR</button>
      </div>
    </div>
    <div id="log-body"></div>
  </div>

</div>

<!-- SIMULATION PORTFOLIO PANEL -->
<div id="sim-panel">
  <div id="sim-header">
    <span>SIMULATION PORTFOLIO <span class="d">// virtual $150 • real prices</span></span>
    <div id="sim-stats-row">
      <span>balance: <span id="sim-wallet" class="sim-val">$150.00</span></span>
      <span>p&amp;l: <span id="sim-pnl" class="sim-val">$0.00</span></span>
      <span>trades: <span id="sim-trades" class="sim-val">0</span></span>
      <span>win rate: <span id="sim-wr" class="sim-val">0.0%</span></span>
      <span>open: <span id="sim-open" class="sim-val">0</span></span>
    </div>
  </div>
  <div id="sim-body">
    <div id="sim-chart-wrap">
      <canvas id="sim-chart"></canvas>
    </div>
    <div id="sim-trades-list"></div>
  </div>
</div>

<!-- FOOTER -->
<div id="footer">
  <div id="f-refresh">last update: <span id="f-time">—</span></div>
  <div style="display:flex;gap:12px;align-items:center">
    <span id="f-learn" class="d">0 adaptations</span>
    <a href="/api/export" download="midusbot_training_data.json">⬇ EXPORT</a>
    <button class="btn-action btn-danger" onclick="resetTraining()">RESET TRAINING</button>
  </div>
</div>

<script>
// ── CHART SETUP ──────────────────────────────────────────────────────────
var chart = null;
try {
  var ctx = document.getElementById('eq-chart').getContext('2d');
  chart = new Chart(ctx, {
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
            label: function(c) { return '$' + c.parsed.y.toFixed(2); }
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
            callback: function(v) { return '$' + v.toFixed(0); }
          },
          border:{ color: '#1c2a38' }
        }
      }
    }
  });
} catch(chartErr) {
  document.getElementById('chart-wrap').innerHTML = '<div style="color:#4a6070;padding:20px;font-size:11px">Chart unavailable</div>';
}

// ── SIM CHART ──────────────────────────────────────────────────────────────
var simChart = null;
try {
  var sctx = document.getElementById('sim-chart').getContext('2d');
  simChart = new Chart(sctx, {
    type: 'line',
    data: {
      datasets: [{
        data: [],
        borderColor: '#00e676',
        borderWidth: 1.5,
        backgroundColor: 'rgba(0,230,118,0.06)',
        fill: true,
        pointRadius: 0,
        tension: 0.2,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } },
          grid: { color: '#0f1a24' },
          ticks: { color: '#2a4060', maxTicksLimit: 4, font: { size: 9 } },
          border: { color: '#1c2a38' }
        },
        y: {
          grid: { color: '#0f1a24' },
          ticks: {
            color: '#2a4060',
            font: { size: 9 },
            callback: function(v) { return '$' + v.toFixed(0); }
          },
          border: { color: '#1c2a38' }
        }
      }
    }
  });
} catch(e) {}

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
    const modeBadge = $('mode-badge');
    modeBadge.textContent = d.mode;
    modeBadge.className   = 'mode-badge ' + (d.mode==='LIVE' ? 'live' : 'dry');
    const toggleBtn = $('mode-toggle');
    if (toggleBtn) {
      toggleBtn.textContent = d.mode === 'LIVE' ? '⇄ SANDBOX' : '⇄ GO LIVE';
      toggleBtn.className = 'btn-action ' + (d.mode === 'LIVE' ? 'btn-dim' : '');
    }
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

    // Edge interval stat block
    if (d.entry_latencies && d.entry_latencies.length > 0) {
      const avg = d.entry_latencies.reduce((a,b)=>a+b,0) / d.entry_latencies.length;
      const best = Math.min(...d.entry_latencies);
      const cls = avg < 10 ? 'g' : avg < 20 ? '' : 'r';
      setC('s-edge-avg', avg.toFixed(1)+'s', 'stat-val '+cls);
      $('s-edge-sub').textContent = avg.toFixed(1);
      $('s-edge-sub').title = 'best: '+best.toFixed(1)+'s  last '+d.entry_latencies.length+' trades';
    }

    // Info strip
    if (d.next_window_secs !== undefined) {
      const nw = d.next_window_secs;
      const cls = nw < 8 ? 'r b' : nw < 20 ? 'stat-val' : 'd';
      setC('i-next-win', nw.toFixed(1)+'s', cls);
    }
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

    // Positions badge
    var posBadge = $('h-pos-badge');
    if (posBadge) posBadge.textContent = (d.positions || []).length;

    // Fees (2 gas txs per round-trip + maker fee on typical position size)
    const typicalPos = d.max_exposure * 0.005;  // ~0.5% of exposure cap
    const feePerTrade = 2 * (d.gas_cost_usdc||0.02) + typicalPos * (d.maker_fee_pct||0) * 2;
    setC('i-fee-per',    '$' + feePerTrade.toFixed(3));
    setC('i-fees-total', '-$' + fees.toFixed(4));

    // Equity curve
    if (chart && d.equity_curve && d.equity_curve.length) {
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

    // Learning progress bar (dry-run only)
    const lp = d.learning_progress;
    const learnWrap = $('learn-bar-wrap');
    if (lp && d.mode !== 'LIVE') {
      learnWrap.classList.add('visible');
      // Next adaptation mini-bar
      const nPct = lp.next_adapt_pct || 0;
      $('lb-adapt-fill').style.width = nPct + '%';
      $('lb-adapt-fill').className = 'lb-fill ' + (nPct < 40 ? 'red' : nPct < 80 ? 'yellow' : 'green');
      $('lb-adapt-val').textContent = lp.closed_since_adapt + '/' + lp.adapt_every_n;
      // Overall ready-to-go-live bar
      const rPct = lp.pct || 0;
      $('lb-ready-fill').style.width = Math.min(rPct, 100) + '%';
      $('lb-ready-fill').className = 'lb-fill ' + (rPct < 40 ? 'red' : rPct < 80 ? 'yellow' : 'green');
      $('lb-ready-val').textContent = Math.min(rPct, 100).toFixed(0) + '%';
      const badge = $('lb-ready-badge');
      if (lp.ready) { badge.style.display = 'inline'; $('lb-ready-val').style.display = 'none'; }
      else           { badge.style.display = 'none';   $('lb-ready-val').style.display = 'inline'; }
    } else if (learnWrap) {
      learnWrap.classList.remove('visible');
    }

    // Sim portfolio panel
    if (d.sim && Object.keys(d.sim).length) {
      const sim = d.sim;
      const simPnl = sim.total_pnl || 0;
      setC('sim-wallet', '$' + (sim.wallet || 100).toFixed(2), 'sim-val b');
      setC('sim-pnl',    (simPnl >= 0 ? '+' : '') + '$' + Math.abs(simPnl).toFixed(2), 'sim-val ' + cc(simPnl));
      setC('sim-trades', sim.total_trades || 0);
      setC('sim-wr',     ((sim.win_rate || 0) * 100).toFixed(1) + '%', 'sim-val ' + ((sim.total_trades || 0) > 3 ? ((sim.win_rate||0) >= 0.5 ? 'g' : 'r') : 'd'));
      setC('sim-open',   sim.open_positions || 0);
      const tradesList = $('sim-trades-list');
      if (tradesList && sim.recent_trades && sim.recent_trades.length) {
        tradesList.innerHTML = sim.recent_trades.slice(0, 5).map(t =>
          `<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--border);font-size:10px">` +
          `<span class="d" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(t.question)}</span>` +
          `<span style="margin:0 6px;color:${t.side==='YES'?'var(--green)':'var(--red)'}">${t.side}</span>` +
          `<span class="${t.win ? 'g' : 'r'}">${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}</span>` +
          `</div>`
        ).join('');
      }
      if (simChart && sim.equity_curve && sim.equity_curve.length) {
        simChart.data.datasets[0].data = sim.equity_curve.map(p => ({ x: p.t, y: p.v }));
        simChart.update('none');
      }
    }

    // Footer
    var _now = new Date();
    $('f-time').textContent = _now.toLocaleDateString(undefined,{year:'numeric',month:'2-digit',day:'2-digit'}) + ' ' + _now.toLocaleTimeString();
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

async function toggleMode() {
  const isLive = $('mode-badge').textContent === 'LIVE';
  const target = isLive ? 'SANDBOX' : 'LIVE';
  if (!confirm(`Switch to ${target} mode?\n\n${target === 'LIVE' ? 'Real money will be at risk.' : 'No real orders will be placed.'}`)) return;
  const r = await fetch('/api/toggle_mode', {method:'POST'});
  const d = await r.json();
  const btn = $('mode-toggle');
  btn.textContent = d.dry_run ? '⇄ GO LIVE' : '⇄ SANDBOX';
  refresh();
}

async function clearLogs() {
  await fetch('/api/clear_logs', {method:'POST'});
  $('log-body').innerHTML = '';
  $('log-count').textContent = '0 entries';
}

async function resetTraining() {
  if (!confirm('Reset all training data?\n\nThis will:\n• Delete trade_journal.json\n• Delete learned_params.json\n• Zero all performance counters\n\nExecution logs are kept. This cannot be undone.')) return;
  const r = await fetch('/api/reset_training', {method:'POST'});
  const d = await r.json();
  alert(d.message || 'Training reset complete.');
  refresh();
}

function tickClock() {
  var n = new Date();
  var pad = function(x){return x<10?'0'+x:x};
  var dateStr = n.getFullYear()+'-'+pad(n.getMonth()+1)+'-'+pad(n.getDate());
  var timeStr = pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds());
  var el = $('h-clock');
  if (el) el.textContent = dateStr + ' ' + timeStr + ' UTC+' + (-n.getTimezoneOffset()/60);
}

tickClock();
setInterval(tickClock, 1000);

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Mobile HTML
# ---------------------------------------------------------------------------

MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>MIDUSBOT Mobile</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root{
  --bg:#080c10;--bg2:#0d1219;--bg3:#111820;
  --border:#1c2a38;--text:#b0c8e0;--dim:#4a6070;
  --green:#00e676;--red:#ff1744;--yellow:#ffab00;
  --blue:#40c4ff;--purple:#e040fb;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Courier New',monospace;font-size:13px;overflow:hidden}

/* ── LAYOUT ── */
#app{display:flex;flex-direction:column;height:100vh}
#header{
  flex-shrink:0;display:flex;align-items:center;justify-content:space-between;
  padding:0 14px;height:48px;background:var(--bg2);
  border-bottom:1px solid var(--border);
}
#hname{color:var(--blue);font-size:15px;letter-spacing:3px;font-weight:bold}
#hright{display:flex;align-items:center;gap:10px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.badge{padding:2px 9px;border-radius:10px;font-size:10px;font-weight:bold}
.dry{background:#001a0a;color:var(--green);border:1px solid var(--green)}
.live{background:#1a0005;color:var(--red);border:1px solid var(--red)}

/* ── TAB CONTENT ── */
#content{flex:1;overflow:hidden;position:relative}
.tab-panel{position:absolute;inset:0;overflow-y:auto;padding:12px;display:none}
.tab-panel.active{display:block}

/* ── STAT GRID ── */
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px}
.card-lbl{color:var(--dim);font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.card-val{font-size:26px;font-weight:bold;line-height:1}
.card-sub{color:var(--dim);font-size:10px;margin-top:4px}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.b{color:var(--blue)}.d{color:var(--dim)}

/* ── INFO ROW ── */
.info-row{
  display:flex;gap:0;overflow-x:auto;background:var(--bg2);
  border:1px solid var(--border);border-radius:6px;margin-bottom:10px;
  font-size:10px;scrollbar-width:none;
}
.info-row::-webkit-scrollbar{display:none}
.info-item{padding:8px 12px;white-space:nowrap;border-right:1px solid var(--border);flex-shrink:0}
.info-item:last-child{border-right:none}
.info-lbl{color:var(--dim);display:block;margin-bottom:2px}
.info-val{color:var(--text);font-weight:bold}

/* ── CHART ── */
.chart-card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;
  padding:10px;margin-bottom:10px}
.chart-hdr{display:flex;justify-content:space-between;font-size:9px;
  color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.chart-wrap{position:relative;height:160px}

/* ── PROGRESS BAR ── */
.prog-card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;
  padding:10px 12px;margin-bottom:10px;display:none}
.prog-card.visible{display:block}
.prog-lbl{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;
  margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.prog-track{height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;margin-bottom:4px}
.prog-fill{height:100%;border-radius:3px;transition:width .5s}
.prog-fill.red{background:var(--red)}.prog-fill.yellow{background:var(--yellow)}.prog-fill.green{background:var(--green)}

/* ── SIM CARD ── */
.sim-card{background:var(--bg2);border:1px solid #1a3a28;border-radius:6px;
  padding:12px;margin-bottom:10px}
.sim-title{color:var(--green);font-size:10px;text-transform:uppercase;
  letter-spacing:1px;margin-bottom:10px;font-weight:bold}
.sim-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px}
.sim-item .sim-lbl{color:var(--dim);font-size:9px;display:block;margin-bottom:2px}
.sim-item .sim-val{font-weight:bold;font-size:14px}
.sim-trades{margin-top:10px;border-top:1px solid var(--border);padding-top:8px}
.sim-trade-row{
  display:flex;justify-content:space-between;padding:4px 0;
  border-bottom:1px solid #0d1219;font-size:10px;
}
.sim-trade-row:last-child{border-bottom:none}

/* ── POSITIONS ── */
.pos-card{
  background:var(--bg2);border:1px solid var(--border);border-radius:6px;
  padding:12px;margin-bottom:8px;
}
.pos-market{font-size:11px;color:var(--text);margin-bottom:8px;line-height:1.4}
.pos-row{display:flex;justify-content:space-between;font-size:10px;margin-bottom:4px}
.pos-lbl{color:var(--dim)}
.sell-btn{
  display:block;width:100%;margin-top:10px;padding:10px;
  background:transparent;border:1px solid var(--red);color:var(--red);
  border-radius:4px;font-family:monospace;font-size:12px;
  letter-spacing:1px;cursor:pointer;text-align:center;
}
.sell-btn:active{background:var(--red);color:var(--bg)}
.no-pos{text-align:center;color:var(--dim);padding:40px 20px;font-size:12px}

/* ── LOG ── */
.log-row{padding:5px 0;border-bottom:1px solid #0d1219;font-size:11px;line-height:1.5}
.log-ts{color:var(--dim);font-size:9px;margin-right:6px}
.k-filled{color:var(--green)}.k-slipped{color:var(--red)}.k-divergence{color:#00e5ff}
.k-arb{color:var(--purple)}.k-scan{color:#2a4060}.k-info{color:var(--dim)}.k-exec{color:var(--text)}
.log-hdr{display:flex;justify-content:space-between;align-items:center;
  margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.clear-btn{
  background:transparent;border:1px solid var(--border);color:var(--dim);
  padding:6px 12px;border-radius:4px;font-family:monospace;font-size:10px;cursor:pointer;
}

/* ── FOOTER / ACTIONS ── */
.actions-card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;
  padding:12px;margin-bottom:10px}
.action-row{display:flex;gap:8px;flex-wrap:wrap}
.act-btn{
  flex:1;min-width:80px;padding:10px 6px;text-align:center;border-radius:4px;
  font-family:monospace;font-size:10px;letter-spacing:1px;cursor:pointer;
  background:transparent;border:1px solid var(--blue);color:var(--blue);
}
.act-btn.danger{border-color:var(--red);color:var(--red)}
.act-btn.dim{border-color:var(--border);color:var(--dim)}
.act-btn:active{opacity:.7}

/* ── BOTTOM NAV ── */
#nav{
  flex-shrink:0;display:flex;background:var(--bg2);
  border-top:1px solid var(--border);
}
.nav-btn{
  flex:1;padding:10px 0 6px;text-align:center;cursor:pointer;
  font-family:monospace;font-size:9px;letter-spacing:1px;
  color:var(--dim);text-transform:uppercase;border:none;background:transparent;
  display:flex;flex-direction:column;align-items:center;gap:2px;
}
.nav-btn .nav-icon{font-size:18px;line-height:1}
.nav-btn.active{color:var(--blue)}
.nav-btn.active .nav-icon{filter:drop-shadow(0 0 4px var(--blue))}

/* ── STATUS BAR ── */
#status-bar{
  text-align:center;font-size:9px;color:var(--dim);
  padding:2px;background:var(--bg);
}
</style>
</head>
<body>
<div id="app">

<!-- HEADER -->
<div id="header">
  <span id="hname">MIDUSBOT</span>
  <div id="hright">
    <span id="m-clock" style="font-size:10px;color:#40c4ff;letter-spacing:.5px;margin-right:6px">—</span>
    <span class="dot"></span>
    <span id="badge" class="badge dry">DRY-RUN</span>
  </div>
</div>

<!-- CONTENT AREA -->
<div id="content">

  <!-- ── TAB: DASHBOARD ── -->
  <div id="tab-dash" class="tab-panel active">

    <!-- Stat grid -->
    <div class="stat-grid">
      <div class="card">
        <div class="card-lbl">Balance</div>
        <div class="card-val b" id="m-balance">$0</div>
        <div class="card-sub" id="m-bal-sub">real wallet</div>
      </div>
      <div class="card">
        <div class="card-lbl">Total P&amp;L</div>
        <div class="card-val" id="m-pnl">$0.00</div>
        <div class="card-sub" id="m-pnl-pct">0.0%</div>
      </div>
      <div class="card">
        <div class="card-lbl">Win Rate</div>
        <div class="card-val" id="m-wr">0%</div>
        <div class="card-sub" id="m-wr-sub">0W / 0T</div>
      </div>
      <div class="card">
        <div class="card-lbl">Daily P&amp;L</div>
        <div class="card-val" id="m-daily">$0.00</div>
        <div class="card-sub d">today</div>
      </div>
      <div class="card">
        <div class="card-lbl">Edge Interval</div>
        <div class="card-val" id="m-edge">—</div>
        <div class="card-sub d">avg t+<span id="m-edge-s">—</span>s</div>
      </div>
      <div class="card">
        <div class="card-lbl">Next Window</div>
        <div class="card-val" id="m-next">—</div>
        <div class="card-sub d">seconds</div>
      </div>
    </div>

    <!-- Info row -->
    <div class="info-row">
      <div class="info-item"><span class="info-lbl">BTC</span><span class="info-val b" id="m-btc">—</span></div>
      <div class="info-item"><span class="info-lbl">Scan</span><span class="info-val" id="m-lat">—</span></div>
      <div class="info-item"><span class="info-lbl">Exposure</span><span class="info-val" id="m-exp">$0</span></div>
      <div class="info-item"><span class="info-lbl">Markets</span><span class="info-val" id="m-mkt">—</span></div>
      <div class="info-item"><span class="info-lbl">Orders</span><span class="info-val" id="m-ord">0</span></div>
      <div class="info-item"><span class="info-lbl">Fees</span><span class="info-val r" id="m-fees">$0</span></div>
      <div class="info-item"><span class="info-lbl">Uptime</span><span class="info-val" id="m-up">—</span></div>
      <div class="info-item"><span class="info-lbl">Loop</span><span class="info-val" id="m-loop">—</span></div>
    </div>

    <!-- Equity chart -->
    <div class="chart-card">
      <div class="chart-hdr">
        <span>Equity Curve</span>
        <span id="m-chart-now" class="b">$0.00</span>
      </div>
      <div class="chart-wrap">
        <canvas id="m-eq-chart"></canvas>
      </div>
    </div>

    <!-- Learning progress -->
    <div id="m-prog-card" class="prog-card">
      <div class="prog-lbl">
        <span>DRY-RUN LEARNING</span>
        <span id="m-adapt-lbl" class="d">0/10</span>
      </div>
      <div class="prog-track"><div class="prog-fill" id="m-adapt-fill" style="width:0%"></div></div>
      <div class="prog-lbl" style="margin-top:6px">
        <span>Ready to go LIVE</span>
        <span id="m-ready-lbl">0%</span>
      </div>
      <div class="prog-track"><div class="prog-fill" id="m-ready-fill" style="width:0%"></div></div>
    </div>

    <!-- Sim portfolio -->
    <div id="m-sim-card" class="sim-card" style="display:none">
      <div class="sim-title">Sim Portfolio // Paper Wallet</div>
      <div class="sim-grid">
        <div class="sim-item"><span class="sim-lbl">Cash</span><span class="sim-val b" id="m-sim-cash">$100</span></div>
        <div class="sim-item"><span class="sim-lbl">Equity</span><span class="sim-val" id="m-sim-eq">$100</span></div>
        <div class="sim-item"><span class="sim-lbl">P&amp;L</span><span class="sim-val" id="m-sim-pnl">$0</span></div>
        <div class="sim-item"><span class="sim-lbl">Win Rate</span><span class="sim-val" id="m-sim-wr">—</span></div>
        <div class="sim-item"><span class="sim-lbl">Trades</span><span class="sim-val" id="m-sim-t">0</span></div>
        <div class="sim-item"><span class="sim-lbl">Open</span><span class="sim-val" id="m-sim-open">0</span></div>
      </div>
      <div class="sim-trades" id="m-sim-trades"></div>
    </div>

    <!-- Actions -->
    <div class="actions-card">
      <div class="action-row">
        <button class="act-btn" onclick="toggleMode()">⇄ MODE</button>
        <a href="/positions" class="act-btn" style="text-decoration:none;display:flex;align-items:center;justify-content:center">POSITIONS <span id="m-pos-badge" style="background:var(--blue);color:var(--bg);border-radius:8px;padding:1px 5px;font-size:9px;margin-left:4px">0</span></a>
        <a href="/" class="act-btn dim" style="text-decoration:none;display:flex;align-items:center;justify-content:center">DESKTOP</a>
      </div>
    </div>

  </div><!-- /tab-dash -->

  <!-- ── TAB: LOG ── -->
  <div id="tab-log" class="tab-panel">
    <div class="log-hdr">
      <span id="m-log-count" class="d">0 entries</span>
      <button class="clear-btn" onclick="clearLogs()">CLEAR</button>
    </div>
    <div id="m-log-body"></div>
  </div>

</div><!-- /content -->

<!-- STATUS BAR -->
<div id="status-bar" id="m-status">connecting…</div>

<!-- BOTTOM NAV -->
<div id="nav">
  <button class="nav-btn active" id="nav-dash" onclick="switchTab('dash')">
    <span class="nav-icon">📊</span>DASHBOARD
  </button>
  <button class="nav-btn" id="nav-log" onclick="switchTab('log')">
    <span class="nav-icon">📜</span>LOG
  </button>
</div>

</div><!-- /app -->

<script>
const $ = id => document.getElementById(id);

// ── Tab switching ──────────────────────────────────────────────────────────
function switchTab(name) {
  ['dash','log'].forEach(t => {
    $(t === name ? 'tab-'+t : 'tab-'+t).classList.toggle('active', t === name);
    $('nav-'+t).classList.toggle('active', t === name);
  });
}

// ── Chart ─────────────────────────────────────────────────────────────────
var chart = null;
try {
  chart = new Chart($('m-eq-chart').getContext('2d'), {
    type:'line',
    data:{datasets:[{data:[],borderColor:'#40c4ff',borderWidth:1.5,
      backgroundColor:'rgba(64,196,255,0.06)',fill:true,pointRadius:0,tension:0.2}]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:false},tooltip:{enabled:false}},
      scales:{
        x:{type:'time',time:{unit:'minute',displayFormats:{minute:'HH:mm'}},
          grid:{color:'#0f1a24'},ticks:{color:'#2a4060',maxTicksLimit:4,font:{size:9}},
          border:{color:'#1c2a38'}},
        y:{grid:{color:'#0f1a24'},
          ticks:{color:'#2a4060',font:{size:9},callback:v=>'$'+v.toFixed(0)},
          border:{color:'#1c2a38'}}
      }
    }
  });
} catch(e){}

// ── Helpers ───────────────────────────────────────────────────────────────
const cc = n => n > 0 ? 'g' : n < 0 ? 'r' : 'd';
function setC(id, text, cls) {
  const el = $(id); if(!el) return;
  el.textContent = text;
  if(cls !== undefined) el.className = cls;
}
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Main refresh ──────────────────────────────────────────────────────────
async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    if(d.status === 'starting') { $('status-bar').textContent = 'starting…'; return; }

    // Header badge
    const badge = $('badge');
    badge.textContent = d.mode;
    badge.className = 'badge ' + (d.mode === 'LIVE' ? 'live' : 'dry');

    // Stat cards
    const pnl  = d.performance.total_pnl;
    const fees = d.performance.total_fees || 0;
    const bal  = d.wallet_balance > 0 ? d.wallet_balance : d.balance;
    const pnlPct = d.seed > 0 ? pnl / d.seed * 100 : 0;

    setC('m-balance', '$' + bal.toFixed(2), 'card-val b');
    setC('m-pnl', (pnl>=0?'+':'')+' $'+Math.abs(pnl).toFixed(2), 'card-val '+cc(pnl));
    $('m-pnl-pct').innerHTML = '<span class="'+cc(pnl)+'">'+(pnl>=0?'+':'')+pnlPct.toFixed(1)+'%</span>';

    const wr = d.performance.win_rate;
    setC('m-wr', (wr*100).toFixed(1)+'%',
      'card-val '+(d.performance.total_trades>5?(wr>=.5?'g':'r'):''));
    setC('m-wr-sub', d.performance.wins+'W / '+d.performance.total_trades+'T');

    const dp = d.daily_pnl;
    setC('m-daily', (dp>=0?'+':'')+' $'+Math.abs(dp).toFixed(2), 'card-val '+cc(dp));

    if(d.entry_latencies && d.entry_latencies.length) {
      const avg = d.entry_latencies.reduce((a,b)=>a+b,0)/d.entry_latencies.length;
      setC('m-edge', avg.toFixed(1)+'s', 'card-val '+(avg<10?'g':avg<20?'':'r'));
      $('m-edge-s').textContent = avg.toFixed(1);
    }

    if(d.next_window_secs !== undefined) {
      const nw = d.next_window_secs;
      setC('m-next', nw.toFixed(0)+'s', 'card-val '+(nw<8?'r':nw<20?'y':'g'));
    }

    // Info row
    setC('m-btc', d.btc_price>0?'$'+d.btc_price.toLocaleString(undefined,{maximumFractionDigits:0}):'—');
    setC('m-lat', d.scan_latency_ms+'ms');
    setC('m-exp', '$'+d.exposure.toFixed(1));
    setC('m-mkt', d.candidates+'/'+d.markets_scanned);
    setC('m-ord', d.orders_placed);
    setC('m-fees', '-$'+fees.toFixed(2));
    setC('m-up',   d.uptime);
    setC('m-loop', '#'+d.loop_count);

    // Positions badge
    $('m-pos-badge').textContent = (d.positions||[]).length;

    // Chart
    if(chart && d.equity_curve && d.equity_curve.length) {
      chart.data.datasets[0].data = d.equity_curve.map(p=>({x:p.t,y:p.v}));
      chart.update('none');
      const last = d.equity_curve[d.equity_curve.length-1].v;
      setC('m-chart-now','$'+last.toFixed(2), cc(last-d.seed));
    }

    // Learning bar
    const lp = d.learning_progress;
    const progCard = $('m-prog-card');
    if(lp && d.mode !== 'LIVE') {
      progCard.classList.add('visible');
      const nPct = lp.next_adapt_pct || 0;
      $('m-adapt-fill').style.width = nPct+'%';
      $('m-adapt-fill').className = 'prog-fill '+(nPct<40?'red':nPct<80?'yellow':'green');
      $('m-adapt-lbl').textContent = lp.closed_since_adapt+'/'+lp.adapt_every_n;
      const rPct = Math.min(lp.pct||0,100);
      $('m-ready-fill').style.width = rPct+'%';
      $('m-ready-fill').className = 'prog-fill '+(rPct<40?'red':rPct<80?'yellow':'green');
      $('m-ready-lbl').textContent = lp.ready ? '✓ READY' : rPct.toFixed(0)+'%';
    } else {
      progCard.classList.remove('visible');
    }

    // Sim card
    if(d.sim && Object.keys(d.sim).length) {
      const sim = d.sim;
      $('m-sim-card').style.display = 'block';
      const simPnl = sim.total_pnl||0;
      setC('m-sim-cash', '$'+(sim.wallet||0).toFixed(2));
      const eq = (sim.wallet||0) + (sim.open_cost||0);
      setC('m-sim-eq',  '$'+eq.toFixed(2));
      setC('m-sim-pnl', (simPnl>=0?'+':'')+' $'+Math.abs(simPnl).toFixed(2), 'sim-val '+cc(simPnl));
      setC('m-sim-wr',  ((sim.win_rate||0)*100).toFixed(1)+'%',
        'sim-val '+((sim.total_trades||0)>3?((sim.win_rate||0)>=.5?'g':'r'):'d'));
      setC('m-sim-t',    sim.total_trades||0);
      setC('m-sim-open', sim.open_positions||0);
      if(sim.recent_trades && sim.recent_trades.length) {
        $('m-sim-trades').innerHTML = sim.recent_trades.slice(0,5).map(t =>
          '<div class="sim-trade-row">'+
          '<span class="d" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+escHtml(t.question)+'</span>'+
          '<span style="margin:0 8px;color:'+(t.side==='YES'?'var(--green)':'var(--red)')+'">'+t.side+'</span>'+
          '<span class="'+(t.win?'g':'r')+'">'+(t.pnl>=0?'+':'')+'$'+t.pnl.toFixed(2)+'</span>'+
          '</div>'
        ).join('');
      }
    }

    // Log
    if(d.exec_log && d.exec_log.length) {
      setC('m-log-count', d.exec_log.length+' entries');
      $('m-log-body').innerHTML = d.exec_log.map(e=>
        '<div class="log-row k-'+e.kind+'">'+
        '<span class="log-ts">'+e.ts+'</span>'+escHtml(e.text)+
        '</div>'
      ).join('');
    }

    var _mn = new Date();
    var _mp = function(x){return x<10?'0'+x:x};
    $('status-bar').textContent = 'updated '+_mn.getFullYear()+'-'+_mp(_mn.getMonth()+1)+'-'+_mp(_mn.getDate())+' '+_mn.toLocaleTimeString();
  } catch(e) {
    $('status-bar').textContent = 'connection error — retrying…';
  }
}

async function toggleMode() {
  const isLive = $('badge').textContent === 'LIVE';
  const target = isLive ? 'SANDBOX' : 'LIVE';
  if(!confirm('Switch to '+target+' mode?\n'+(target==='LIVE'?'Real money will be at risk.':'No real orders.'))) return;
  await fetch('/api/toggle_mode',{method:'POST'});
  refresh();
}

async function clearLogs() {
  await fetch('/api/clear_logs',{method:'POST'});
  $('m-log-body').innerHTML='';
  setC('m-log-count','0 entries');
}

function tickClockMobile() {
  var n = new Date();
  var pad = function(x){return x<10?'0'+x:x};
  var el = $('m-clock');
  if (el) el.textContent = n.getFullYear()+'-'+pad(n.getMonth()+1)+'-'+pad(n.getDate())+' '+pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds());
}

tickClockMobile();
setInterval(tickClockMobile, 1000);

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""
