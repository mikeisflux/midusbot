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

from flask import Flask, jsonify, Response, request, current_app

import config

if TYPE_CHECKING:
    from src.dashboard import DashboardState
    from src.learner import AdaptiveLearner

logging.getLogger("werkzeug").setLevel(logging.ERROR)

app      = Flask(__name__)
_state:   DashboardState | None = None
_learner: AdaptiveLearner | None = None
_close_position_fn = None   # injected by bot: fn(token_id) -> bool
_sim: "SimPortfolio | None" = None

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    resp = app.make_response(HTML)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/mobile")
def mobile():
    resp = app.make_response(MOBILE_HTML)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/state")
def api_state():
    if _state is None:
        return jsonify({"status": "starting"})
    return jsonify(_build(  _state))

@app.route("/api/toggle_mode", methods=["POST"])
def api_toggle_mode():
    """Toggle between DRY_RUN (sandbox) and live mode. Updates .env and in-process config."""
    from pathlib import Path
    new_dry_run = not config.DRY_RUN
    config.DRY_RUN = new_dry_run

    # Persist to .env so it survives a restart
    env_path = Path(".env")
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        found = False
        new_lines = []
        for line in lines:
            if line.startswith("DRY_RUN="):
                new_lines.append(f"DRY_RUN={'false' if not new_dry_run else 'true'}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"DRY_RUN={'false' if not new_dry_run else 'true'}")
        env_path.write_text("\n".join(new_lines) + "\n")

    mode = "SANDBOX" if new_dry_run else "LIVE"
    if _state:
        _state.add_exec_log("info", f"Mode switched to {mode}")
        # When switching to LIVE, reset the equity curve seed to the actual
        # wallet balance so P&L is relative to real starting funds, not the
        # DRY_RUN simulation baseline.
        if not new_dry_run and _state.wallet_balance > 0:
            _state._seed        = _state.wallet_balance
            _state.total_pnl    = 0.0
            _state.total_trades = 0
            _state.wins         = 0
            _state.total_fees   = 0.0
            _state.daily_pnl    = 0.0
            _state.pnl_history  = []
            _state.equity_curve = []
            _state.add_equity_point()
            _state.add_exec_log("info",
                f"P&L reset — baseline set to wallet: ${_state.wallet_balance:.2f} USDC")
    return jsonify({"ok": True, "dry_run": new_dry_run, "mode": mode})


@app.route("/positions")
def positions_page():
    if _state is None:
        return "<p>Bot starting...</p>"
    s = _state
    rows = ""
    for pos, cur in s.positions:
        pnl = pos.shares * (cur - pos.entry_price)
        pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100 if pos.entry_price else 0
        pnl_color = "#00e676" if pnl >= 0 else "#ff1744"
        sign = "+" if pnl >= 0 else ""
        side_color = "#00e676" if pos.side == "YES" else "#ff1744"
        rows += (
            '<tr>'
            '<td style="max-width:320px;white-space:normal">' + pos.question + '</td>'
            '<td style="color:' + side_color + '">' + pos.side + '</td>'
            '<td>' + str(round(pos.shares, 2)) + '</td>'
            '<td>' + str(round(pos.entry_price * 100, 1)) + 'c</td>'
            '<td>' + str(round(cur * 100, 1)) + 'c</td>'
            '<td>$' + str(round(pos.cost_usdc, 2)) + '</td>'
            '<td style="color:' + pnl_color + '">' + sign + '$' + str(round(abs(pnl), 2)) + ' (' + sign + str(round(pnl_pct, 1)) + '%)</td>'
            '<td><button onclick="sellPos(\'' + pos.token_id + '\',this)" style="background:transparent;border:1px solid #ff1744;color:#ff1744;padding:4px 14px;border-radius:3px;cursor:pointer;font-family:monospace;font-size:11px">SELL</button></td>'
            '</tr>'
        )
    if not rows:
        rows = '<tr><td colspan="8" style="text-align:center;color:#4a6070;padding:40px">No open positions</td></tr>'

    page = '''<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>MIDUSBOT // POSITIONS</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080c10;color:#b0c8e0;font-family:"Courier New",monospace;font-size:12px;padding:20px}
h1{color:#40c4ff;letter-spacing:3px;font-size:14px;margin-bottom:16px}
table{width:100%;border-collapse:collapse}
th{color:#4a6070;text-align:left;padding:8px 12px;border-bottom:1px solid #1c2a38;font-size:10px;text-transform:uppercase;letter-spacing:1px}
td{padding:8px 12px;border-bottom:1px solid #111820;vertical-align:middle}
tr:hover td{background:#0d1219}
.btn{color:#40c4ff;text-decoration:none;border:1px solid #40c4ff;padding:4px 14px;border-radius:3px;font-size:10px;letter-spacing:1px;cursor:pointer;background:transparent;font-family:inherit;margin-right:8px}
.btn:hover{background:#40c4ff;color:#080c10}
</style></head><body>
<div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
  <h1>MIDUSBOT // OPEN POSITIONS</h1>
  <a href="/" class="btn">HOME</a>
</div>
<table>
<thead><tr>
  <th>Market</th><th>Side</th><th>Shares</th><th>Entry</th><th>Now</th><th>Cost</th><th>P&L</th><th>Action</th>
</tr></thead>
<tbody>''' + rows + '''</tbody></table>
<script>
async function sellPos(tokenId, btn) {
  if (!confirm("Sell this position?")) return;
  _selling = true;
  btn.disabled = true;
  btn.textContent = "SELLING...";
  try {
    var r = await fetch("/api/close_position", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token_id: tokenId})
    });
    var d = await r.json();
    if (d.ok) {
      btn.textContent = "SOLD";
      btn.style.color = "#00e676";
      btn.style.borderColor = "#00e676";
      btn.closest("tr").style.opacity = "0.4";
    } else {
      alert("Sell failed: " + (d.error || "unknown"));
      btn.disabled = false;
      btn.textContent = "SELL";
    }
  } catch(e) {
    alert("Error: " + e);
    btn.disabled = false;
    btn.textContent = "SELL";
  }
  _selling = false;
}
// Auto-refresh every 30 seconds, but only if no sell is in progress
var _selling = false;
function scheduleReload() {
  setTimeout(function(){ if (!_selling) location.reload(); else scheduleReload(); }, 30000);
}
scheduleReload();
</script>
</body></html>'''

    resp = current_app.make_response(page)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    """Manually close (sell) an open position by token_id."""
    if _close_position_fn is None:
        return jsonify({"ok": False, "error": "close_position not wired"})
    data = request.get_json(silent=True) or {}
    token_id = data.get("token_id", "").strip()
    if not token_id:
        return jsonify({"ok": False, "error": "token_id required"})
    try:
        ok = _close_position_fn(token_id, manual=True)
        return jsonify({"ok": bool(ok)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/clear_logs", methods=["POST"])
def api_clear_logs():
    """Clear the in-memory execution log shown in the right panel."""
    if _state is not None:
        _state.exec_log = []
    return jsonify({"ok": True})


@app.route("/api/reset_training", methods=["POST"])
def api_reset_training():
    """
    Wipe trade journal + learned params (files + in-memory).
    Performance stats on the dashboard are also zeroed.
    Logs are kept intact.
    """
    if _learner is not None:
        _learner.reset()
    if _state is not None:
        _state.reset_training_stats()
    return jsonify({"ok": True, "message": "Training data cleared. Learner reset to factory defaults."})


@app.route("/api/export")
def api_export():
    """Download full training data as a JSON file for analysis."""
    from pathlib import Path

    def _load_json(path):
        p = Path(path)
        if p.exists():
            try:
                with open(p) as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    journal_main   = _load_json("data/journal_main.json")   or []
    params_main    = _load_json("data/params_main.json")    or {}
    trend_state    = _load_json("data/trend_state.json")    or {}
    analyst_params  = _load_json("data/analyst_params.json")  or {}
    analyst_history = _load_json("data/analyst_history.json") or []

    # Compute win/loss breakdown from journals
    def _wl(journal):
        closed = [r for r in journal if r.get("closed")]
        wins   = [r for r in closed  if r.get("pnl_usdc", 0) > 0]
        losses = [r for r in closed  if r.get("pnl_usdc", 0) <= 0]
        total_pnl = sum(r.get("pnl_usdc", 0) for r in closed)
        return {
            "total": len(closed),
            "wins":  len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0,
            "total_pnl_usdc": round(total_pnl, 4),
            "best_trade":  round(max((r.get("pnl_usdc",0) for r in closed), default=0), 4),
            "worst_trade": round(min((r.get("pnl_usdc",0) for r in closed), default=0), 4),
            "avg_pnl":     round(total_pnl / len(closed), 4) if closed else 0,
        }

    payload = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "mode":        "DRY_RUN" if config.DRY_RUN else "LIVE",
        "config": {
            "dry_run":            config.DRY_RUN,
            "max_position_usdc":  config.MAX_POSITION_USDC,
            "max_total_exposure": config.MAX_TOTAL_EXPOSURE_USDC,
            "kelly_fraction":     config.KELLY_FRACTION,
            "min_edge":           config.MIN_EDGE,
        },
        "performance": {
            "total_trades": _state.total_trades if _state else 0,
            "wins":         _state.wins         if _state else 0,
            "losses":       (_state.total_trades - _state.wins) if _state else 0,
            "total_pnl":    _state.total_pnl    if _state else 0,
            "total_fees":   _state.total_fees   if _state else 0,
            "win_rate":     _state.win_rate      if _state else 0,
            "best_trade":   _state.best_trade    if _state else 0,
            "worst_trade":  _state.worst_trade   if _state else 0,
        },
        "journal_stats":   _wl(journal_main),
        "trend_state":     trend_state,
        "learned_params":  params_main,
        "analyst_params":  analyst_params,
        "analyst_history": analyst_history[-10:],
        "equity_curve":    _state.equity_curve if _state else [],
        "trade_journal":   journal_main,
    }

    blob = json.dumps(payload, indent=2)
    return Response(
        blob,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=midusbot_export.json"},
    )

# ---------------------------------------------------------------------------
# State serialiser
# ---------------------------------------------------------------------------

def _learning_progress() -> dict:
    """Returns learning progress data for the dry-run progress bar."""
    from src.learner import ADAPT_EVERY_N
    if _learner is None or _state is None:
        return {"closed_since_adapt": 0, "adapt_every_n": ADAPT_EVERY_N,
                "adaptation_count": 0, "total_trades": 0, "win_rate": 0,
                "ready": False, "pct": 0}

    closed  = _learner._closed_since_adapt
    total   = _state.total_trades
    wins    = _state.wins
    adapted = _learner._adaptation_count
    wr      = _state.win_rate

    # "Ready to go live" = at least 20 dry-run trades and win rate >= 60%
    # (matches _DRY_RECOVER_WIN_RATE and _DRY_MIN_TRADES in bot.py)
    MIN_TRADES = 20
    MIN_WIN_RATE = 0.60
    ready = total >= MIN_TRADES and wr >= MIN_WIN_RATE

    # Overall progress toward going live (0-100)
    trade_pct = min(total / MIN_TRADES * 100, 100)
    wr_pct    = min(wr / MIN_WIN_RATE * 100, 100) if total > 0 else 0
    overall   = (trade_pct + wr_pct) / 2

    return {
        "closed_since_adapt": closed,
        "adapt_every_n":      ADAPT_EVERY_N,
        "adaptation_count":   adapted,
        "total_trades":       total,
        "win_rate":           wr,
        "ready":              ready,
        "pct":                round(overall, 1),
        "next_adapt_pct":     round(closed / ADAPT_EVERY_N * 100, 0) if ADAPT_EVERY_N else 0,
    }


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
                "token_id":      pos.token_id,
                "question":      pos.question[:60],
                "side":          pos.side,
                "shares":        pos.shares,
                "entry_price":   pos.entry_price,
                "current_price": cur,
                "cost_usdc":     pos.cost_usdc,
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
        "learning_progress": _learning_progress(),
        "sim": s.sim_stats if s.sim_stats else {},
        "entry_latencies": s.entry_latencies,
        "next_window_secs": round(300 - (time.time() % 300), 1),
    }

# ---------------------------------------------------------------------------
# Start helper
# ---------------------------------------------------------------------------

def start(state: DashboardState, port: int = 8080, learner: AdaptiveLearner | None = None, close_position_fn=None, sim=None) -> None:
    global _state, _learner, _close_position_fn, _sim
    _state              = state
    _learner            = learner
    _close_position_fn  = close_position_fn
    _sim                = sim
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
        name="webui",
    )
    t.start()


from src.webui_html import HTML, MOBILE_HTML  # noqa: E402, F401
