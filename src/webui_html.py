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
  --s-chain:#40c4ff;--s-arb:#4caf50;--s-mom:#ff9800;--s-mm:#9c27b0;--s-news:#26c6da;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);
  font-family:'Courier New',monospace;font-size:13px}

/* ── TOP-LEVEL LAYOUT ── */
#app{display:flex;flex-direction:column;height:100vh;min-height:0}

/* ── HEADER ── */
#hdr{
  flex-shrink:0;display:flex;align-items:center;gap:10px;
  height:40px;padding:0 14px;
  background:var(--bg2);border-bottom:1px solid var(--border);
}
#hdr-name{color:var(--blue);font-size:15px;letter-spacing:4px;font-weight:bold;margin-right:2px}
.hdot{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--green);animation:blink 2s infinite;flex-shrink:0}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.badge{padding:2px 8px;border-radius:10px;font-size:9px;font-weight:bold;letter-spacing:1px;flex-shrink:0}
.badge.dry{background:#001a0a;color:var(--green);border:1px solid var(--green)}
.badge.live{background:#1a0005;color:var(--red);border:1px solid var(--red)}
#h-clock{font-size:10px;color:var(--cyan);letter-spacing:.5px}
.hval{font-size:10px;color:var(--dim)}
.hval b{color:var(--text)}
#hdr-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.hbtn{
  padding:4px 12px;border-radius:4px;font-family:monospace;font-size:10px;
  letter-spacing:1px;cursor:pointer;background:transparent;
  border:1px solid var(--border);color:var(--text);white-space:nowrap;
  text-decoration:none;display:inline-flex;align-items:center;gap:5px;
}
.hbtn:hover{border-color:var(--blue);color:var(--blue)}
#pos-btn{border-color:var(--blue);color:var(--blue)}
#pos-btn:hover{background:rgba(64,196,255,.1)}
.pos-cnt{
  background:var(--blue);color:var(--bg);border-radius:8px;
  padding:1px 5px;font-size:9px;font-weight:bold;
}

/* ── STATS ROW ── */
#stats-row{
  flex-shrink:0;display:flex;gap:0;
  background:var(--bg2);border-bottom:1px solid var(--border);
}
.stat-blk{
  flex:1;padding:6px 14px;border-right:1px solid var(--border);
}
.stat-blk:last-child{border-right:none}
.stat-lbl{font-size:8px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:2px}
.stat-val{font-size:18px;font-weight:bold;line-height:1}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}
.b{color:var(--blue)}.d{color:var(--dim)}.c{color:var(--cyan)}

/* ── INFO STRIP ── */
#info-strip{
  flex-shrink:0;display:flex;gap:0;align-items:stretch;
  background:var(--bg3);border-bottom:1px solid var(--border);
  font-size:10px;
}
.info-item{
  padding:5px 14px;border-right:1px solid var(--border);
  display:flex;flex-direction:column;justify-content:center;
}
.info-item:last-child{border-right:none}
.info-lbl{color:var(--dim);font-size:8px;text-transform:uppercase;letter-spacing:.8px;margin-bottom:1px}
.info-val{color:var(--text);font-weight:bold}

/* ── STRATEGY GRID ── */
#strat-section{
  flex-shrink:0;padding:10px 12px;
  background:var(--bg);border-bottom:1px solid var(--border);
}
#strat-grid{
  display:grid;grid-template-columns:1fr 1fr;gap:10px;
}
#sc-news_arb{grid-column:1 / -1}  /* news arb spans full width */

/* ── STRATEGY CARD ── */
.sc{
  background:var(--bg2);border:1px solid var(--border);
  border-radius:6px;padding:14px 16px;
  display:flex;flex-direction:column;gap:8px;
  position:relative;overflow:hidden;
  transition:border-color .2s;
}
.sc:hover{border-color:var(--sc-color,var(--blue))}
.sc::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--sc-color,var(--blue));
}
.sc-header{
  display:flex;align-items:center;justify-content:space-between;
}
.sc-title{
  font-size:11px;font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;
  color:var(--sc-color,var(--blue));display:flex;align-items:center;gap:6px;
}
.sc-icon{font-size:14px;line-height:1}
.sc-alloc{
  font-size:9px;color:var(--dim);background:var(--bg3);
  padding:2px 7px;border-radius:8px;border:1px solid var(--border);
  letter-spacing:.5px;
}
.sc-wr-row{display:flex;align-items:baseline;gap:10px}
.sc-wr{font-size:32px;font-weight:bold;line-height:1;letter-spacing:-1px}
.sc-wl{font-size:13px;color:var(--dim)}
.sc-wl b{color:var(--text)}
.sc-pnl-row{display:flex;align-items:center;justify-content:space-between}
.sc-pnl{font-size:20px;font-weight:bold}
.sc-status{
  font-size:8px;letter-spacing:1.5px;text-transform:uppercase;
  padding:2px 8px;border-radius:3px;font-weight:bold;
}
.sc-status.active{background:rgba(0,230,118,.12);color:var(--green);border:1px solid rgba(0,230,118,.25)}
.sc-status.empty{background:var(--bg3);color:var(--dim);border:1px solid var(--border)}
.sc-bar-row{display:flex;flex-direction:column;gap:4px}
.sc-bar-labels{display:flex;justify-content:space-between;font-size:9px;color:var(--dim)}
.sc-bar-track{height:6px;background:var(--bg4);border-radius:3px;overflow:hidden}
.sc-bar-fill{height:6px;border-radius:3px;transition:width .5s;background:var(--sc-color,var(--blue))}
.sc-meta-row{display:flex;gap:0;border-top:1px solid var(--border);padding-top:6px;margin-top:2px}
.sc-meta-item{flex:1;border-right:1px solid var(--border);padding-right:10px;margin-right:10px}
.sc-meta-item:last-child{border-right:none;padding-right:0;margin-right:0}
.sc-meta-lbl{font-size:8px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px}
.sc-meta-val{font-size:12px;font-weight:bold}

/* ── BOTTOM ROW ── */
#bottom-row{
  flex:1;min-height:0;display:flex;gap:0;
  border-top:1px solid var(--border);
}

/* equity */
#equity-panel{
  flex:1;min-width:0;display:flex;flex-direction:column;
  background:var(--bg2);border-right:1px solid var(--border);
}
#equity-hdr{
  flex-shrink:0;display:flex;align-items:center;justify-content:space-between;
  padding:8px 14px;border-bottom:1px solid var(--border);
}
.panel-title{font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:var(--dim)}
#eq-now{font-size:14px;font-weight:bold;color:var(--blue)}
#equity-wrap{flex:1;min-height:0;padding:8px 10px 6px}

/* log */
#log-panel{
  width:360px;flex-shrink:0;display:flex;flex-direction:column;
  background:var(--bg2);
}
#log-hdr{
  flex-shrink:0;display:flex;align-items:center;justify-content:space-between;
  padding:8px 12px;border-bottom:1px solid var(--border);
}
#log-count{font-size:9px;color:var(--dim);letter-spacing:.5px}
.small-btn{
  padding:3px 8px;border-radius:3px;font-family:monospace;font-size:9px;
  cursor:pointer;background:transparent;border:1px solid var(--border);
  color:var(--dim);letter-spacing:.5px;
}
.small-btn:hover{border-color:var(--text);color:var(--text)}
#log-body{
  flex:1;overflow-y:auto;padding:4px 0;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;
}
#log-body::-webkit-scrollbar{width:4px}
#log-body::-webkit-scrollbar-track{background:transparent}
#log-body::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.log-row{
  padding:4px 12px;border-bottom:1px solid rgba(28,42,56,.5);
  font-size:10px;line-height:1.5;
}
.log-row:last-child{border-bottom:none}
.log-ts{color:var(--dim);font-size:8px;margin-right:5px}
.k-filled{color:var(--green)}.k-slipped{color:var(--red)}
.k-divergence{color:var(--cyan)}.k-arb{color:var(--purple)}
.k-scan{color:#1e3248}.k-info{color:var(--dim)}.k-exec{color:var(--text)}

/* ── LIVE READINESS BAR ── */
#readiness-bar{
  flex-shrink:0;display:flex;align-items:center;gap:12px;
  padding:6px 14px;background:var(--bg2);border-bottom:1px solid var(--border);
}
#readiness-label{font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:var(--dim);white-space:nowrap}
#readiness-track{
  flex:1;height:8px;background:var(--bg4);border-radius:4px;overflow:hidden;
  position:relative;
}
#readiness-fill{
  height:8px;border-radius:4px;transition:width .8s ease;width:0%;
  background:linear-gradient(90deg,var(--red),var(--yellow) 55%,var(--green));
}
#readiness-pct{font-size:11px;font-weight:bold;min-width:36px;text-align:right}
.strat-ready{
  display:flex;align-items:center;gap:4px;font-size:9px;color:var(--dim);
  white-space:nowrap;
}
.strat-ready-bar{width:40px;height:4px;background:var(--bg4);border-radius:2px;overflow:hidden}
.strat-ready-fill{height:4px;border-radius:2px;transition:width .8s}
#readiness-detail{display:flex;gap:14px}
#go-live-badge{
  display:none;font-size:9px;font-weight:bold;letter-spacing:1px;
  padding:2px 10px;border-radius:3px;
  background:rgba(0,230,118,.15);color:var(--green);border:1px solid rgba(0,230,118,.4);
  white-space:nowrap;
}

/* ── FOOTER ── */
#footer{
  flex-shrink:0;display:flex;align-items:center;justify-content:space-between;
  height:28px;padding:0 14px;
  background:var(--bg2);border-top:1px solid var(--border);
  font-size:9px;color:var(--dim);
}
#footer-ts{letter-spacing:.3px}
#footer-right{display:flex;gap:8px}
.foot-btn{
  padding:2px 10px;border-radius:3px;font-family:monospace;font-size:9px;
  cursor:pointer;background:transparent;letter-spacing:.5px;
}
#export-btn{border:1px solid var(--border);color:var(--dim)}
#export-btn:hover{border-color:var(--blue);color:var(--blue)}
#reset-btn{border:1px solid var(--border);color:var(--dim)}
#reset-btn:hover{border-color:var(--red);color:var(--red)}
</style>
</head>
<body>
<div id="app">

<!-- ═══════════════ HEADER ═══════════════ -->
<div id="hdr">
  <span id="hdr-name">MIDUSBOT</span>
  <span class="hdot"></span>
  <span id="h-badge" class="badge dry">DRY-RUN</span>
  <span id="h-clock" class="hval">—</span>
  <span class="hval">UP <b id="h-uptime">—</b></span>
  <span class="hval">CYCLE <b id="h-cycle">#0</b></span>
  <span class="hval">MKT <b id="h-markets">—</b></span>
  <div id="hdr-right">
    <button class="hbtn" onclick="toggleMode()">⇄ SANDBOX</button>
    <a href="/positions" class="hbtn" id="pos-btn">
      POSITIONS <span class="pos-cnt" id="h-pos-cnt">0</span>
    </a>
  </div>
</div>

<!-- ═══════════════ STATS ROW ═══════════════ -->
<div id="stats-row">
  <div class="stat-blk">
    <div class="stat-lbl">Balance</div>
    <div class="stat-val b" id="s-balance">$0.00</div>
  </div>
  <div class="stat-blk">
    <div class="stat-lbl">Total P&amp;L</div>
    <div class="stat-val" id="s-pnl">$0.00</div>
  </div>
  <div class="stat-blk">
    <div class="stat-lbl">Win Rate</div>
    <div class="stat-val" id="s-wr">0%</div>
  </div>
  <div class="stat-blk">
    <div class="stat-lbl">Daily P&amp;L</div>
    <div class="stat-val" id="s-daily">$0.00</div>
  </div>
  <div class="stat-blk">
    <div class="stat-lbl">Exposure Used</div>
    <div class="stat-val" id="s-exp">$0.00</div>
  </div>
</div>

<!-- ═══════════════ INFO STRIP ═══════════════ -->
<div id="info-strip">
  <div class="info-item">
    <span class="info-lbl">BTC Price</span>
    <span class="info-val b" id="i-btc">—</span>
  </div>
  <div class="info-item">
    <span class="info-lbl">Next Window</span>
    <span class="info-val" id="i-next">—</span>
  </div>
  <div class="info-item">
    <span class="info-lbl">Positions</span>
    <span class="info-val" id="i-pos">0</span>
  </div>
  <div class="info-item">
    <span class="info-lbl">Latency</span>
    <span class="info-val" id="i-lat">—</span>
  </div>
  <div class="info-item">
    <span class="info-lbl">Risk %</span>
    <span class="info-val" id="i-risk">0%</span>
  </div>
</div>

<!-- ═══════════════ LIVE READINESS ═══════════════ -->
<div id="readiness-bar">
  <span id="readiness-label">LIVE READINESS</span>
  <div id="readiness-track">
    <div id="readiness-fill"></div>
  </div>
  <span id="readiness-pct" class="d">0%</span>
  <div id="readiness-detail">
    <div class="strat-ready">
      <span style="color:var(--s-chain)">⛓</span>
      <div class="strat-ready-bar"><div class="strat-ready-fill" id="rd-chainlink" style="background:var(--s-chain)"></div></div>
      <span id="rd-chainlink-pct">0%</span>
    </div>
    <div class="strat-ready">
      <span style="color:var(--s-arb)">⚖</span>
      <div class="strat-ready-bar"><div class="strat-ready-fill" id="rd-arb" style="background:var(--s-arb)"></div></div>
      <span id="rd-arb-pct">0%</span>
    </div>
    <div class="strat-ready">
      <span style="color:var(--s-mom)">🚀</span>
      <div class="strat-ready-bar"><div class="strat-ready-fill" id="rd-momentum" style="background:var(--s-mom)"></div></div>
      <span id="rd-momentum-pct">0%</span>
    </div>
    <div class="strat-ready">
      <span style="color:var(--s-mm)">🏦</span>
      <div class="strat-ready-bar"><div class="strat-ready-fill" id="rd-mm" style="background:var(--s-mm)"></div></div>
      <span id="rd-mm-pct">0%</span>
    </div>
    <div class="strat-ready">
      <span style="color:var(--s-news)">📡</span>
      <div class="strat-ready-bar"><div class="strat-ready-fill" id="rd-news_arb" style="background:var(--s-news)"></div></div>
      <span id="rd-news_arb-pct">0%</span>
    </div>
  </div>
  <span id="go-live-badge">✓ READY TO GO LIVE</span>
</div>

<!-- ═══════════════ STRATEGY GRID ═══════════════ -->
<div id="strat-section">
  <div id="strat-grid">

    <!-- CHAINLINK ORACLE -->
    <div class="sc" id="sc-chainlink" style="--sc-color:var(--s-chain)">
      <div class="sc-header">
        <div class="sc-title">
          <span class="sc-icon">&#9935;</span>CHAINLINK ORACLE
        </div>
        <div class="sc-alloc" id="sc-chainlink-alloc">15%</div>
      </div>
      <div class="sc-wr-row">
        <div class="sc-wr b" id="sc-chainlink-wr">—</div>
        <div class="sc-wl">WIN RATE &nbsp;|&nbsp; <b id="sc-chainlink-wl">0W / 0L</b></div>
      </div>
      <div class="sc-pnl-row">
        <div class="sc-pnl d" id="sc-chainlink-pnl">$0.00</div>
        <div class="sc-status empty" id="sc-chainlink-status">NO TRADES YET</div>
      </div>
      <div class="sc-bar-row">
        <div class="sc-bar-labels">
          <span id="sc-chainlink-exp-lbl">$0 exposure</span>
          <span id="sc-chainlink-tgt-lbl">$0 target</span>
        </div>
        <div class="sc-bar-track">
          <div class="sc-bar-fill" id="sc-chainlink-bar" style="width:0%"></div>
        </div>
      </div>
      <div class="sc-meta-row">
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Avg P&amp;L</div>
          <div class="sc-meta-val d" id="sc-chainlink-avg">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Best Trade</div>
          <div class="sc-meta-val d" id="sc-chainlink-best">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Worst Trade</div>
          <div class="sc-meta-val d" id="sc-chainlink-worst">$0.00</div>
        </div>
      </div>
    </div>

    <!-- ARBITRAGE -->
    <div class="sc" id="sc-arb" style="--sc-color:var(--s-arb)">
      <div class="sc-header">
        <div class="sc-title">
          <span class="sc-icon">&#9878;</span>ARBITRAGE
        </div>
        <div class="sc-alloc" id="sc-arb-alloc">30%</div>
      </div>
      <div class="sc-wr-row">
        <div class="sc-wr g" id="sc-arb-wr">—</div>
        <div class="sc-wl">WIN RATE &nbsp;|&nbsp; <b id="sc-arb-wl">0W / 0L</b></div>
      </div>
      <div class="sc-pnl-row">
        <div class="sc-pnl d" id="sc-arb-pnl">$0.00</div>
        <div class="sc-status empty" id="sc-arb-status">NO TRADES YET</div>
      </div>
      <div class="sc-bar-row">
        <div class="sc-bar-labels">
          <span id="sc-arb-exp-lbl">$0 exposure</span>
          <span id="sc-arb-tgt-lbl">$0 target</span>
        </div>
        <div class="sc-bar-track">
          <div class="sc-bar-fill" id="sc-arb-bar" style="width:0%;background:var(--s-arb)"></div>
        </div>
      </div>
      <div class="sc-meta-row">
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Avg P&amp;L</div>
          <div class="sc-meta-val d" id="sc-arb-avg">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Best Trade</div>
          <div class="sc-meta-val d" id="sc-arb-best">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Worst Trade</div>
          <div class="sc-meta-val d" id="sc-arb-worst">$0.00</div>
        </div>
      </div>
    </div>

    <!-- AI / MOMENTUM -->
    <div class="sc" id="sc-momentum" style="--sc-color:var(--s-mom)">
      <div class="sc-header">
        <div class="sc-title">
          <span class="sc-icon">&#128640;</span>AI / MOMENTUM
        </div>
        <div class="sc-alloc" id="sc-momentum-alloc">35%</div>
      </div>
      <div class="sc-wr-row">
        <div class="sc-wr y" id="sc-momentum-wr">—</div>
        <div class="sc-wl">WIN RATE &nbsp;|&nbsp; <b id="sc-momentum-wl">0W / 0L</b></div>
      </div>
      <div class="sc-pnl-row">
        <div class="sc-pnl d" id="sc-momentum-pnl">$0.00</div>
        <div class="sc-status empty" id="sc-momentum-status">NO TRADES YET</div>
      </div>
      <div class="sc-bar-row">
        <div class="sc-bar-labels">
          <span id="sc-momentum-exp-lbl">$0 exposure</span>
          <span id="sc-momentum-tgt-lbl">$0 target</span>
        </div>
        <div class="sc-bar-track">
          <div class="sc-bar-fill" id="sc-momentum-bar" style="width:0%;background:var(--s-mom)"></div>
        </div>
      </div>
      <div class="sc-meta-row">
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Avg P&amp;L</div>
          <div class="sc-meta-val d" id="sc-momentum-avg">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Best Trade</div>
          <div class="sc-meta-val d" id="sc-momentum-best">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Worst Trade</div>
          <div class="sc-meta-val d" id="sc-momentum-worst">$0.00</div>
        </div>
      </div>
    </div>

    <!-- MARKET MAKING -->
    <div class="sc" id="sc-mm" style="--sc-color:var(--s-mm)">
      <div class="sc-header">
        <div class="sc-title">
          <span class="sc-icon">&#127970;</span>MARKET MAKING
        </div>
        <div class="sc-alloc" id="sc-mm-alloc">20%</div>
      </div>
      <div class="sc-wr-row">
        <div class="sc-wr" style="color:var(--s-mm)" id="sc-mm-wr">—</div>
        <div class="sc-wl">WIN RATE &nbsp;|&nbsp; <b id="sc-mm-wl">0W / 0L</b></div>
      </div>
      <div class="sc-pnl-row">
        <div class="sc-pnl d" id="sc-mm-pnl">$0.00</div>
        <div class="sc-status empty" id="sc-mm-status">NO TRADES YET</div>
      </div>
      <div class="sc-bar-row">
        <div class="sc-bar-labels">
          <span id="sc-mm-exp-lbl">$0 exposure</span>
          <span id="sc-mm-tgt-lbl">$0 target</span>
        </div>
        <div class="sc-bar-track">
          <div class="sc-bar-fill" id="sc-mm-bar" style="width:0%;background:var(--s-mm)"></div>
        </div>
      </div>
      <div class="sc-meta-row">
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Avg P&amp;L</div>
          <div class="sc-meta-val d" id="sc-mm-avg">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Best Trade</div>
          <div class="sc-meta-val d" id="sc-mm-best">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Worst Trade</div>
          <div class="sc-meta-val d" id="sc-mm-worst">$0.00</div>
        </div>
      </div>
    </div>

    <!-- AP/REUTERS NEWS ARB -->
    <div class="sc" id="sc-news_arb" style="--sc-color:var(--s-news)">
      <div class="sc-header">
        <div class="sc-title">
          <span class="sc-icon">&#128225;</span>AP / REUTERS NEWS ARB
        </div>
        <div class="sc-alloc" id="sc-news_arb-alloc">15%</div>
      </div>
      <div class="sc-wr-row">
        <div class="sc-wr" style="color:var(--s-news)" id="sc-news_arb-wr">—</div>
        <div class="sc-wl">WIN RATE &nbsp;|&nbsp; <b id="sc-news_arb-wl">0W / 0L</b></div>
      </div>
      <div class="sc-pnl-row">
        <div class="sc-pnl d" id="sc-news_arb-pnl">$0.00</div>
        <div class="sc-status empty" id="sc-news_arb-status">SCANNING FEEDS</div>
      </div>
      <div class="sc-bar-row">
        <div class="sc-bar-labels">
          <span id="sc-news_arb-exp-lbl">$0 exposure</span>
          <span id="sc-news_arb-tgt-lbl">$0 target</span>
        </div>
        <div class="sc-bar-track">
          <div class="sc-bar-fill" id="sc-news_arb-bar" style="width:0%;background:var(--s-news)"></div>
        </div>
      </div>
      <div class="sc-meta-row">
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Avg P&amp;L</div>
          <div class="sc-meta-val d" id="sc-news_arb-avg">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Best Trade</div>
          <div class="sc-meta-val d" id="sc-news_arb-best">$0.00</div>
        </div>
        <div class="sc-meta-item">
          <div class="sc-meta-lbl">Worst Trade</div>
          <div class="sc-meta-val d" id="sc-news_arb-worst">$0.00</div>
        </div>
      </div>
    </div>

  </div><!-- /strat-grid -->
</div><!-- /strat-section -->

<!-- ═══════════════ BOTTOM ROW ═══════════════ -->
<div id="bottom-row">

  <!-- EQUITY CURVE -->
  <div id="equity-panel">
    <div id="equity-hdr">
      <span class="panel-title">EQUITY CURVE // PORTFOLIO VALUE</span>
      <span id="eq-now">$0.00</span>
    </div>
    <div id="equity-wrap">
      <canvas id="eq-chart"></canvas>
    </div>
  </div>

  <!-- EXECUTION LOG -->
  <div id="log-panel">
    <div id="log-hdr">
      <span id="log-count" class="d">0 entries</span>
      <button class="small-btn" onclick="clearLogs()">CLEAR</button>
    </div>
    <div id="log-body"></div>
  </div>

</div><!-- /bottom-row -->

<!-- ═══════════════ FOOTER ═══════════════ -->
<div id="footer">
  <span id="footer-ts">connecting…</span>
  <div id="footer-right">
    <button class="foot-btn" id="export-btn" onclick="exportData()">&#8595; EXPORT</button>
    <button class="foot-btn" id="reset-btn" onclick="resetTraining()">RESET TRAINING</button>
  </div>
</div>

</div><!-- /app -->

<script>
const $ = id => document.getElementById(id);

// ── helpers ──────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function fmt$(n) {
  const a = Math.abs(n);
  return (n >= 0 ? '+$' : '-$') + a.toFixed(2);
}
function cc(n) { return n > 0 ? 'g' : n < 0 ? 'r' : 'd'; }
function wrClass(wr, trades) {
  if (trades < 3) return 'd';
  return wr >= 0.60 ? 'g' : wr >= 0.45 ? 'y' : 'r';
}

// ── clock ─────────────────────────────────────────────────────────────────────
function tickClock() {
  var n = new Date();
  var p = function(x){ return x < 10 ? '0' + x : x; };
  var el = $('h-clock');
  if (el) el.textContent =
    n.getFullYear()+'-'+p(n.getMonth()+1)+'-'+p(n.getDate())+
    ' '+p(n.getHours())+':'+p(n.getMinutes())+':'+p(n.getSeconds());
}

// ── equity chart ──────────────────────────────────────────────────────────────
var eqChart = null;
(function initChart() {
  try {
    var ctx = $('eq-chart').getContext('2d');
    eqChart = new Chart(ctx, {
      type: 'line',
      data: {
        datasets: [{
          data: [],
          borderColor: '#40c4ff',
          borderWidth: 1.5,
          backgroundColor: 'rgba(64,196,255,0.07)',
          fill: true,
          pointRadius: 0,
          tension: 0.2
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
            grid: { color: '#0a1520' },
            ticks: { color: '#2a4060', maxTicksLimit: 6, font: { size: 9 } },
            border: { color: '#1c2a38' }
          },
          y: {
            grid: { color: '#0a1520' },
            ticks: {
              color: '#2a4060', font: { size: 9 },
              callback: function(v) { return '$' + v.toFixed(0); }
            },
            border: { color: '#1c2a38' }
          }
        }
      }
    });
  } catch(e) {}
})();

// ── main refresh (every 2s) ───────────────────────────────────────────────────
var _lastSeed = 0;
var _lastPositions = [];

async function refresh() {
  try {
    var r = await fetch('/api/state');
    var d = await r.json();
    if (d.status === 'starting') {
      $('footer-ts').textContent = 'starting…';
      return;
    }

    // header
    var badge = $('h-badge');
    var isLive = d.mode === 'LIVE';
    badge.textContent = isLive ? 'LIVE' : 'DRY-RUN';
    badge.className = 'badge ' + (isLive ? 'live' : 'dry');
    $('h-uptime').textContent = d.uptime || '—';
    $('h-cycle').textContent = '#' + (d.loop_count || 0);
    $('h-markets').textContent = (d.candidates || 0) + '/' + (d.markets_scanned || 0);

    var pos = d.positions || [];
    $('h-pos-cnt').textContent = pos.length;
    _lastPositions = pos;

    // stats row
    var bal = d.wallet_balance > 0 ? d.wallet_balance : (d.balance || 0);
    var pnl = (d.performance && d.performance.total_pnl) || 0;
    var wr  = (d.performance && d.performance.win_rate)  || 0;
    var wins= (d.performance && d.performance.wins)      || 0;
    var tot = (d.performance && d.performance.total_trades) || 0;
    var dp  = d.daily_pnl || 0;
    var exp = d.exposure  || 0;

    $('s-balance').textContent = '$' + bal.toFixed(2);
    $('s-balance').className   = 'stat-val b';

    var pnlEl = $('s-pnl');
    pnlEl.textContent = fmt$(pnl);
    pnlEl.className   = 'stat-val ' + cc(pnl);

    var wrEl = $('s-wr');
    wrEl.textContent = (wr * 100).toFixed(1) + '%  ' + wins + 'W/' + tot + 'T';
    wrEl.className   = 'stat-val ' + wrClass(wr, tot);

    var dpEl = $('s-daily');
    dpEl.textContent = fmt$(dp);
    dpEl.className   = 'stat-val ' + cc(dp);

    $('s-exp').textContent = '$' + exp.toFixed(2);
    $('s-exp').className   = 'stat-val ' + (exp > 0 ? 'y' : 'd');

    // info strip
    $('i-btc').textContent  = d.btc_price > 0
      ? '$' + d.btc_price.toLocaleString(undefined, { maximumFractionDigits: 0 })
      : '—';
    var nw = d.next_window_secs;
    var nwEl = $('i-next');
    if (nw !== undefined && nw !== null) {
      nwEl.textContent = nw.toFixed(0) + 's';
      nwEl.className   = 'info-val ' + (nw < 8 ? 'r' : nw < 20 ? 'y' : 'g');
    } else {
      nwEl.textContent = '—';
      nwEl.className   = 'info-val d';
    }
    $('i-pos').textContent = pos.length;
    $('i-lat').textContent = (d.scan_latency_ms || 0) + 'ms';

    if (_lastSeed <= 0) _lastSeed = d.seed || bal;
    var riskPct = _lastSeed > 0 ? (exp / _lastSeed * 100) : 0;
    $('i-risk').textContent = riskPct.toFixed(1) + '%';
    $('i-risk').className   = 'info-val ' + (riskPct > 50 ? 'r' : riskPct > 25 ? 'y' : 'g');

    // equity chart
    if (eqChart && d.equity_curve && d.equity_curve.length) {
      eqChart.data.datasets[0].data = d.equity_curve.map(function(p) {
        return { x: p.t, y: p.v };
      });
      eqChart.update('none');
      var last = d.equity_curve[d.equity_curve.length - 1].v;
      var eqEl = $('eq-now');
      eqEl.textContent = '$' + last.toFixed(2);
      eqEl.className   = last >= (_lastSeed || last) ? 'g' : 'r';
    }

    // execution log
    if (d.exec_log && d.exec_log.length) {
      $('log-count').textContent = d.exec_log.length + ' entries';
      $('log-body').innerHTML = d.exec_log.slice().reverse().map(function(e) {
        return '<div class="log-row k-' + escHtml(e.kind) + '">' +
          '<span class="log-ts">' + escHtml(e.ts) + '</span>' +
          escHtml(e.text) + '</div>';
      }).join('');
    }

    // footer timestamp
    var _n = new Date();
    var _p = function(x){ return x < 10 ? '0' + x : x; };
    $('footer-ts').textContent =
      'updated ' + _n.getFullYear()+'-'+_p(_n.getMonth()+1)+'-'+_p(_n.getDate())+
      ' '+_n.toLocaleTimeString();

  } catch(e) {
    $('footer-ts').textContent = 'connection error — retrying…';
  }
}

// ── strategy stats (every 8s) ─────────────────────────────────────────────────
var _strategyKeys = ['chainlink','arb','momentum','mm','news_arb'];
var _strategyDefaults = {
  chainlink: { color: '#40c4ff', targetPct: 13, label: 'CHAINLINK ORACLE'     },
  arb:       { color: '#4caf50', targetPct: 25, label: 'ARBITRAGE'            },
  momentum:  { color: '#ff9800', targetPct: 30, label: 'AI / MOMENTUM'        },
  mm:        { color: '#9c27b0', targetPct: 17, label: 'MARKET MAKING'        },
  news_arb:  { color: '#26c6da', targetPct: 15, label: 'AP/REUTERS NEWS ARB'  }
};

function updateStratCard(key, s, walletBal) {
  var def = _strategyDefaults[key] || {};
  var trades  = s.trades || 0;
  var wins    = s.wins   || 0;
  var losses  = s.losses || 0;
  var wr      = s.win_rate || 0;
  var pnl     = s.total_pnl || 0;
  var avg     = s.avg_pnl || 0;
  var best    = s.best    || 0;
  var worst   = s.worst   || 0;
  var tgtPct  = (s.target_pct !== undefined ? s.target_pct : def.targetPct) || 0;

  // win rate element
  var wrEl = $('sc-' + key + '-wr');
  if (wrEl) {
    wrEl.textContent = trades > 0 ? (wr * 100).toFixed(1) + '%' : '—';
    wrEl.className   = 'sc-wr ' + wrClass(wr, trades);
  }

  // W/L
  var wlEl = $('sc-' + key + '-wl');
  if (wlEl) wlEl.textContent = wins + 'W / ' + losses + 'L';

  // P&L
  var pnlEl = $('sc-' + key + '-pnl');
  if (pnlEl) {
    pnlEl.textContent = fmt$(pnl);
    pnlEl.className   = 'sc-pnl ' + cc(pnl);
  }

  // status badge
  var stEl = $('sc-' + key + '-status');
  if (stEl) {
    if (trades > 0) {
      stEl.textContent = 'ACTIVE';
      stEl.className   = 'sc-status active';
    } else {
      stEl.textContent = 'NO TRADES YET';
      stEl.className   = 'sc-status empty';
    }
  }

  // allocation badge
  var allocEl = $('sc-' + key + '-alloc');
  if (allocEl) allocEl.textContent = tgtPct + '%';

  // budget bar — compute current exposure from live positions
  var tgtAmt = walletBal * (tgtPct / 100);
  var exposed = 0;
  _lastPositions.forEach(function(p) {
    if (p.strategy === key) exposed += (p.cost_usdc || 0);
  });
  var barPct = tgtAmt > 0 ? Math.min(100, exposed / tgtAmt * 100) : 0;

  var barEl = $('sc-' + key + '-bar');
  if (barEl) barEl.style.width = barPct.toFixed(1) + '%';

  var expLblEl = $('sc-' + key + '-exp-lbl');
  if (expLblEl) expLblEl.textContent = '$' + exposed.toFixed(0) + ' exposure';

  var tgtLblEl = $('sc-' + key + '-tgt-lbl');
  if (tgtLblEl) tgtLblEl.textContent = '$' + tgtAmt.toFixed(0) + ' target';

  // meta
  var avgEl = $('sc-' + key + '-avg');
  if (avgEl) {
    avgEl.textContent = fmt$(avg);
    avgEl.className   = 'sc-meta-val ' + cc(avg);
  }
  var bestEl = $('sc-' + key + '-best');
  if (bestEl) {
    bestEl.textContent = fmt$(best);
    bestEl.className   = 'sc-meta-val ' + cc(best);
  }
  var worstEl = $('sc-' + key + '-worst');
  if (worstEl) {
    worstEl.textContent = fmt$(worst);
    worstEl.className   = 'sc-meta-val ' + cc(worst);
  }
}

async function loadStrategyStats() {
  try {
    var r = await fetch('/api/strategy_stats');
    var data = await r.json();
    var bal = 0;
    try {
      var sr = await fetch('/api/state');
      var sd = await sr.json();
      bal = sd.wallet_balance > 0 ? sd.wallet_balance : (sd.balance || 0);
    } catch(e) {}

    _strategyKeys.forEach(function(key) {
      var s = data[key];
      if (s) updateStratCard(key, s, bal);
    });

    // ── readiness bar ────────────────────────────────────────────────────────
    var port = data['portfolio'];
    var overall = port ? (port.readiness_pct || 0) : 0;
    var fillEl  = $('readiness-fill');
    var pctEl   = $('readiness-pct');
    if (fillEl) fillEl.style.width = overall.toFixed(1) + '%';
    if (pctEl) {
      pctEl.textContent = overall.toFixed(0) + '%';
      pctEl.className = overall >= 100 ? 'g' : overall >= 60 ? 'y' : 'r';
    }
    _strategyKeys.forEach(function(key) {
      var s = data[key];
      if (!s) return;
      var rPct = s.readiness_pct || 0;
      var fillEl2 = $('rd-' + key);
      var lblEl  = $('rd-' + key + '-pct');
      if (fillEl2) fillEl2.style.width = rPct.toFixed(1) + '%';
      if (lblEl)  lblEl.textContent = rPct.toFixed(0) + '%';
    });
    var badge = $('go-live-badge');
    if (badge) badge.style.display = overall >= 100 ? 'inline-block' : 'none';

  } catch(e) {}
}

// ── controls ──────────────────────────────────────────────────────────────────
async function toggleMode() {
  var isLive = $('h-badge').textContent.trim() === 'LIVE';
  var target = isLive ? 'SANDBOX' : 'LIVE';
  if (!confirm(
    'Switch to ' + target + ' mode?\n' +
    (target === 'LIVE' ? 'Real money will be at risk.' : 'No real orders will be placed.')
  )) return;
  await fetch('/api/toggle_mode', { method: 'POST' });
  refresh();
}

async function clearLogs() {
  await fetch('/api/clear_logs', { method: 'POST' });
  $('log-body').innerHTML = '';
  $('log-count').textContent = '0 entries';
}

async function resetTraining() {
  if (!confirm('Reset all training data and ML weights?\nThis cannot be undone.')) return;
  try {
    await fetch('/api/reset_training', { method: 'POST' });
    $('footer-ts').textContent = 'training data reset.';
  } catch(e) {}
}

function exportData() {
  window.location.href = '/api/export';
}

// ── bootstrap ─────────────────────────────────────────────────────────────────
tickClock();
setInterval(tickClock, 1000);

refresh();
setInterval(refresh, 2000);

loadStrategyStats();
setInterval(loadStrategyStats, 8000);
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
        <div class="card-lbl">Next Window</div>
        <div class="card-val" id="m-next">—</div>
        <div class="card-sub d">seconds</div>
      </div>
      <div class="card">
        <div class="card-lbl">Exposure</div>
        <div class="card-val" id="m-exp">$0</div>
        <div class="card-sub d">deployed</div>
      </div>
    </div>

    <!-- Info row -->
    <div class="info-row">
      <div class="info-item"><span class="info-lbl">BTC</span><span class="info-val b" id="m-btc">—</span></div>
      <div class="info-item"><span class="info-lbl">Scan</span><span class="info-val" id="m-lat">—</span></div>
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

    <!-- Actions -->
    <div class="actions-card">
      <div class="action-row">
        <button class="act-btn" onclick="toggleMode()">⇄ MODE</button>
        <a href="/positions" class="act-btn" style="text-decoration:none;display:flex;align-items:center;justify-content:center">POSITIONS <span id="m-pos-badge" style="background:var(--blue);color:var(--bg);border-radius:8px;padding:1px 5px;font-size:9px;margin-left:4px">0</span></a>
        <a href="/" class="act-btn dim" style="text-decoration:none;display:flex;align-items:center;justify-content:center">DESKTOP</a>
      </div>
    </div>

  </div><!-- /tab-dash -->

  <!-- ── TAB: PORTFOLIO ── -->
  <div id="tab-port" class="tab-panel">
    <div style="font-size:10px;color:var(--dim);margin-bottom:10px;text-transform:uppercase;letter-spacing:1px">
      Aggressive Portfolio — Win/Loss by Strategy
    </div>
    <div id="port-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px"></div>
    <div style="margin-top:14px;font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px">Allocation Targets</div>
    <div id="port-alloc" style="margin-top:6px"></div>
  </div>

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
  <button class="nav-btn" id="nav-port" onclick="switchTab('port')">
    <span class="nav-icon">⚡</span>PORTFOLIO
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
  ['dash','port','log'].forEach(t => {
    $('tab-'+t).classList.toggle('active', t === name);
    $('nav-'+t).classList.toggle('active', t === name);
  });
  if (name === 'port') loadPortfolio();
}

// ── Portfolio stats ────────────────────────────────────────────────────────
const STRATEGY_COLORS = {
  chainlink: '#3b9eff',
  arb:       '#4caf50',
  momentum:  '#ff9800',
  mm:        '#9c27b0',
  portfolio: '#ffffff',
};
const STRATEGY_ICONS = {
  chainlink: '⛓',
  arb:       '⚖',
  momentum:  '🚀',
  mm:        '🏦',
  portfolio: '📊',
};

function loadPortfolio() {
  fetch('/api/strategy_stats').then(r => r.json()).then(data => {
    const grid = $('port-grid');
    const order = ['momentum','chainlink','arb','mm','portfolio'];
    grid.innerHTML = order.map(key => {
      const s = data[key];
      if (!s) return '';
      const col = STRATEGY_COLORS[key] || '#888';
      const icon = STRATEGY_ICONS[key] || '·';
      const wr = s.trades > 0 ? (s.win_rate * 100).toFixed(1) : '--';
      const wrColor = s.trades > 3 ? (s.win_rate >= 0.55 ? '#4caf50' : s.win_rate >= 0.45 ? '#ff9800' : '#f44336') : '#888';
      const pnlColor = (s.total_pnl || 0) >= 0 ? '#4caf50' : '#f44336';
      const isPortfolio = key === 'portfolio';
      return `
        <div style="background:var(--bg2);border:1px solid ${isPortfolio ? col+'44' : 'var(--border)'};
             border-radius:6px;padding:10px;${isPortfolio ? 'grid-column:1/-1' : ''}">
          <div style="color:${col};font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">
            ${icon} ${s.label || key} &nbsp;<span style="color:var(--dim)">${s.target_pct}%</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px">
            <div>
              <div style="font-size:18px;font-weight:bold;color:${wrColor}">${wr}%</div>
              <div style="color:var(--dim);font-size:9px">WIN RATE</div>
            </div>
            <div>
              <div style="font-size:18px;font-weight:bold">${s.wins || 0}W/${s.losses || 0}L</div>
              <div style="color:var(--dim);font-size:9px">TRADES</div>
            </div>
            <div>
              <div style="font-size:18px;font-weight:bold;color:${pnlColor}">$${(s.total_pnl||0).toFixed(2)}</div>
              <div style="color:var(--dim);font-size:9px">TOTAL PNL</div>
            </div>
            <div>
              <div style="font-size:18px;font-weight:bold">$${(s.avg_pnl||0).toFixed(2)}</div>
              <div style="color:var(--dim);font-size:9px">AVG/TRADE</div>
            </div>
          </div>
          ${s.trades > 0 ? `
          <div style="margin-top:6px;background:var(--bg3);border-radius:3px;height:4px;overflow:hidden">
            <div style="height:4px;background:${wrColor};width:${Math.min(100,s.win_rate*100).toFixed(0)}%"></div>
          </div>` : ''}
        </div>`;
    }).join('');

    // Allocation bar
    const alloc = $('port-alloc');
    const keys = ['momentum','chainlink','arb','mm'];
    alloc.innerHTML = keys.map(key => {
      const s = data[key];
      if (!s) return '';
      const col = STRATEGY_COLORS[key] || '#888';
      const pct = s.target_pct || 0;
      return `
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <div style="width:80px;color:var(--dim);font-size:10px">${STRATEGY_ICONS[key]} ${key}</div>
          <div style="flex:1;background:var(--bg3);border-radius:3px;height:8px">
            <div style="height:8px;background:${col};border-radius:3px;width:${pct}%"></div>
          </div>
          <div style="width:30px;text-align:right;font-size:10px">${pct}%</div>
        </div>`;
    }).join('');
  }).catch(() => {
    $('port-grid').innerHTML = '<div style="color:var(--dim);padding:20px">No strategy data yet — trades will appear here as they close.</div>';
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

    if(d.next_window_secs !== undefined) {
      const nw = d.next_window_secs;
      setC('m-next', nw.toFixed(0)+'s', 'card-val '+(nw<8?'r':nw<20?'y':'g'));
    }

    setC('m-exp', '$'+(d.exposure||0).toFixed(1), 'card-val y');

    // Info row
    setC('m-btc', d.btc_price>0?'$'+d.btc_price.toLocaleString(undefined,{maximumFractionDigits:0}):'—');
    setC('m-lat', d.scan_latency_ms+'ms');
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
