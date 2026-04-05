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

import html
import logging
import threading
import time
from typing import TYPE_CHECKING

from flask import Flask, jsonify, current_app

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
# Main page routes
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
            '<td style="max-width:320px;white-space:normal">' + html.escape(pos.question) + '</td>'
            '<td style="color:' + side_color + '">' + html.escape(pos.side) + '</td>'
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

    MIN_TRADES = 20
    MIN_WIN_RATE = 0.60
    ready = total >= MIN_TRADES and wr >= MIN_WIN_RATE

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
        "session_stats":    _session_stats_data(),
        "asset_pnl":        _asset_pnl_data(),
        "risk_of_ruin":     _risk_of_ruin_data(),
    }


def _session_stats_data() -> dict:
    """Per-asset signal accuracy from session_tracker for dashboard panel."""
    try:
        from src.session_tracker import get_all_stats
        return get_all_stats(lookback=100)
    except Exception:
        return {}


def _asset_pnl_data() -> dict:
    """Per-asset P&L breakdown from journal."""
    if _learner is None:
        return {}
    try:
        from src.strategy import _detect_updown_market
        breakdown: dict[str, dict] = {}
        for r in getattr(_learner, "_journal", []):
            if not r.closed:
                continue
            sym = _detect_updown_market(r.question) or "OTHER"
            if sym not in breakdown:
                breakdown[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
            breakdown[sym]["trades"] += 1
            breakdown[sym]["pnl"]    += r.pnl_usdc
            if r.pnl_usdc > 0:
                breakdown[sym]["wins"] += 1
        for sym, d in breakdown.items():
            d["win_rate"] = round(d["wins"] / d["trades"], 3) if d["trades"] else 0.0
            d["pnl"]      = round(d["pnl"], 4)
        return breakdown
    except Exception:
        return {}


def _risk_of_ruin_data() -> dict:
    """Compute risk of ruin for current performance parameters."""
    if _state is None or _learner is None:
        return {}
    try:
        wr = _state.win_rate
        if wr <= 0 or _state.total_trades < 5:
            return {}
        kelly = config.KELLY_FRACTION
        ror = _learner.risk_params  # has kelly_multiplier
        effective_kelly = kelly * getattr(ror, "kelly_multiplier", 1.0) * 0.12
        from src.risk import RiskManager
        rm = RiskManager()
        prob = rm.risk_of_ruin(wr, effective_kelly, n_bets=200)
        return {"win_rate": round(wr, 3), "kelly_fraction": round(effective_kelly, 4), "ruin_prob": round(prob, 3)}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# New API routes for session stats, analyst history, asset P&L
# ---------------------------------------------------------------------------

@app.route("/api/session_stats")
def api_session_stats():
    return jsonify(_session_stats_data())


@app.route("/api/asset_pnl")
def api_asset_pnl():
    return jsonify(_asset_pnl_data())


@app.route("/api/analyst_history")
def api_analyst_history():
    try:
        import json as _json
        from pathlib import Path as _Path
        hist_file = _Path("data/analyst_history.json")
        if hist_file.exists():
            return jsonify(_json.loads(hist_file.read_text())[-20:])
        return jsonify([])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/timing_accuracy")
def api_timing_accuracy():
    """Per-asset signal accuracy by seconds-into-window."""
    try:
        from src.session_tracker import get_timing_accuracy
        assets = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
        return jsonify({a: get_timing_accuracy(a, lookback=200) for a in assets})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/alpha_decay")
def api_alpha_decay():
    if _learner is None:
        return jsonify({})
    try:
        return jsonify(_learner.alpha_decay_report())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/feature_importance")
def api_feature_importance():
    if _learner is None:
        return jsonify({})
    try:
        return jsonify(_learner.feature_importance())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Start helper
# ---------------------------------------------------------------------------

def start(state: DashboardState, port: int = 8080, learner: AdaptiveLearner | None = None, close_position_fn=None, sim=None) -> None:
    global _state, _learner, _close_position_fn, _sim
    _state              = state
    _learner            = learner
    _close_position_fn  = close_position_fn
    _sim                = sim

    # Register API routes from webui_api
    import src.webui_api as _api
    _api.register(app, lambda: _state, lambda: _learner, lambda: _close_position_fn)

    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
        name="webui",
    )
    t.start()


from src.webui_html import HTML, MOBILE_HTML  # noqa: E402, F401
