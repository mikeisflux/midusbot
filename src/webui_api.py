"""
REST API endpoints for the web dashboard.
Registered on the shared Flask `app` instance from src.webui via register().
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from flask import jsonify, Response, request

import config

if TYPE_CHECKING:
    from src.dashboard import DashboardState
    from src.learner import AdaptiveLearner


def register(app, get_state, get_learner, get_close_fn):
    """
    Register all /api/* routes on `app`.

    Parameters
    ----------
    app          : Flask application instance
    get_state    : callable → DashboardState | None
    get_learner  : callable → AdaptiveLearner | None
    get_close_fn : callable → close_position_fn | None
    """

    @app.route("/api/state")
    def api_state():
        from src.webui import _build
        state = get_state()
        if state is None:
            return jsonify({"status": "starting"})
        return jsonify(_build(state))

    @app.route("/api/toggle_mode", methods=["POST"])
    def api_toggle_mode():
        """Toggle between DRY_RUN (sandbox) and live mode.
        Routes through bot._set_mode() so position cleanup happens correctly:
        - LIVE→DRY-RUN: closes all open real positions before switching
        - DRY-RUN→LIVE: discards sim state and re-reconciles from CLOB
        """
        state = get_state()
        # Use bot._set_mode if available (handles position cleanup)
        from src.bot import PolymarketBot
        import gc
        bot_instance = next(
            (obj for obj in gc.get_objects() if isinstance(obj, PolymarketBot)),
            None
        )
        new_dry_run = not config.DRY_RUN
        mode = "SANDBOX" if new_dry_run else "LIVE"

        if bot_instance is not None:
            # Full safe switch with position cleanup
            bot_instance._set_mode(new_dry_run, reason="manual dashboard toggle")
        else:
            # Fallback: just flip the flag (bot not running, no positions to clean up)
            from pathlib import Path
            config.DRY_RUN = new_dry_run
            env_path = Path(".env")
            lines = env_path.read_text().splitlines() if env_path.exists() else []
            new_lines, found = [], False
            for line in lines:
                if line.startswith("DRY_RUN="):
                    new_lines.append(f"DRY_RUN={'true' if new_dry_run else 'false'}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"DRY_RUN={'true' if new_dry_run else 'false'}")
            env_path.write_text("\n".join(new_lines) + "\n")
            if state:
                state.add_exec_log("info", f"Mode switched to {mode}")

        return jsonify({"ok": True, "dry_run": new_dry_run, "mode": mode})

    @app.route("/api/close_position", methods=["POST"])
    def api_close_position():
        """Manually close (sell) an open position by token_id."""
        close_fn = get_close_fn()
        if close_fn is None:
            return jsonify({"ok": False, "error": "close_position not wired"})
        data = request.get_json(silent=True) or {}
        token_id = data.get("token_id", "").strip()
        if not token_id:
            return jsonify({"ok": False, "error": "token_id required"})
        try:
            ok = close_fn(token_id, manual=True)
            return jsonify({"ok": bool(ok)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/api/clear_logs", methods=["POST"])
    def api_clear_logs():
        """Clear the in-memory execution log shown in the right panel."""
        state = get_state()
        if state is not None:
            state.exec_log = []
        return jsonify({"ok": True})

    @app.route("/api/reset_training", methods=["POST"])
    def api_reset_training():
        """
        Wipe trade journal + learned params (files + in-memory).
        Performance stats on the dashboard are also zeroed.
        Logs are kept intact.
        """
        learner = get_learner()
        state   = get_state()
        if learner is not None:
            learner.reset()
        if state is not None:
            state.reset_training_stats()
        return jsonify({"ok": True, "message": "Training data cleared. Learner reset to factory defaults."})

    @app.route("/api/strategy_stats")
    def api_strategy_stats():
        """
        Per-strategy win/loss breakdown for the portfolio dashboard.
        Groups journal records by their 'strategy' field (momentum, chainlink, arb, mm).
        Returns stats for each strategy plus the combined portfolio.
        """
        from pathlib import Path
        journal_path = Path("data/journal_main.json")
        records = []
        if journal_path.exists():
            try:
                with open(journal_path) as f:
                    records = json.load(f)
            except Exception:
                pass

        # Only count live trades in strategy stats — dry-run paper trades
        # inflate win rates and distort portfolio allocation decisions.
        closed = [
            r for r in records
            if r.get("closed") and "pnl_usdc" in r and not r.get("dry_run", False)
        ]
        # Fall back to all trades (including paper) if no live trades exist yet
        if not closed:
            closed = [r for r in records if r.get("closed") and "pnl_usdc" in r]

        strategies = {
            "momentum":  {"label": "Momentum / Oracle-lag", "target_pct": 30},
            "chainlink": {"label": "Chainlink Oracle Arb",  "target_pct": 13},
            "arb":       {"label": "Dual-side Arbitrage",   "target_pct": 25},
            "mm":        {"label": "Market Making",         "target_pct": 17},
            "news_arb":  {"label": "AP/Reuters News Arb",   "target_pct": 15},
        }

        def _stats(trades):
            if not trades:
                return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                        "total_pnl": 0, "avg_pnl": 0, "best": 0, "worst": 0}
            wins   = [t for t in trades if t.get("pnl_usdc", 0) > 0]
            losses = [t for t in trades if t.get("pnl_usdc", 0) <= 0]
            pnls   = [t.get("pnl_usdc", 0) for t in trades]
            return {
                "trades":    len(trades),
                "wins":      len(wins),
                "losses":    len(losses),
                "win_rate":  round(len(wins) / len(trades), 4),
                "total_pnl": round(sum(pnls), 4),
                "avg_pnl":   round(sum(pnls) / len(trades), 4),
                "best":      round(max(pnls), 4),
                "worst":     round(min(pnls), 4),
            }

        _LIVE_MIN_TRADES  = 20   # trades needed per strategy before going live
        _LIVE_MIN_WINRATE = 0.55 # win rate needed to be considered ready

        def _readiness(s):
            """0–100 readiness score. 60% weight on trade count, 40% on win rate."""
            t = s.get("trades", 0)
            wr = s.get("win_rate", 0)
            t_pct  = min(t / _LIVE_MIN_TRADES, 1.0)
            wr_pct = min(wr / _LIVE_MIN_WINRATE, 1.0) if t >= 5 else 0.0
            return round((t_pct * 0.6 + wr_pct * 0.4) * 100, 1)

        result = {}
        for key, meta in strategies.items():
            bucket = [r for r in closed if r.get("strategy", "momentum") == key]
            s = _stats(bucket)
            result[key] = {**meta, **s, "readiness_pct": _readiness(s),
                           "live_min_trades": _LIVE_MIN_TRADES,
                           "live_min_winrate": _LIVE_MIN_WINRATE}

        # Catch records without a strategy field → default to momentum
        untagged = [r for r in closed if "strategy" not in r]
        if untagged:
            combined = [r for r in closed if r.get("strategy", "momentum") == "momentum"]
            s = _stats(combined)
            result["momentum"] = {**strategies["momentum"], **s,
                                  "readiness_pct": _readiness(s),
                                  "live_min_trades": _LIVE_MIN_TRADES,
                                  "live_min_winrate": _LIVE_MIN_WINRATE}

        port_s = _stats(closed)
        overall_readiness = round(
            sum(result[k]["readiness_pct"] for k in result if k != "portfolio") / len(strategies), 1
        )
        result["portfolio"] = {
            "label": "Combined Portfolio",
            "target_pct": 100,
            **port_s,
            "readiness_pct": overall_readiness,
            "live_min_trades": _LIVE_MIN_TRADES * len(strategies),
            "live_min_winrate": _LIVE_MIN_WINRATE,
        }

        return json.dumps(result), 200, {"Content-Type": "application/json"}

    @app.route("/api/backtest")
    def api_backtest():
        """
        Run journal-based backtest across all 4 strategies.
        Query params:
          days=30       — lookback window (default 30)
          strategy=arb  — filter to one strategy (default: all)
          dry_run=true  — only sim trades (default: all)
        """
        from flask import request as _req
        from src.backtest import BacktestEngine

        try:
            days    = float(_req.args.get("days", 30))
            strat   = _req.args.get("strategy", None)
            dr_arg  = _req.args.get("dry_run", None)
            dry_run = None
            if dr_arg is not None:
                dry_run = dr_arg.lower() == "true"

            engine = BacktestEngine()
            r = engine.run_journal(
                lookback_days=days,
                strategies=[strat] if strat else None,
                dry_run=dry_run,
            )
            payload = {
                "trades":      r.n_trades,
                "wins":        r.wins,
                "win_rate":    round(r.win_rate, 4),
                "total_pnl":   round(r.total_pnl, 4),
                "sharpe":      r.sharpe(),
                "by_strategy": r.by_strategy,
                "by_asset":    r.by_asset,
                "lookback_days": days,
            }
            return json.dumps(payload), 200, {"Content-Type": "application/json"}
        except Exception as exc:
            return json.dumps({"error": str(exc)}), 500, {"Content-Type": "application/json"}

    @app.route("/webhook/cryptopanic", methods=["POST"])
    def webhook_cryptopanic():
        """
        Receives real-time news pushes from CryptoPanic's bot/webhook system.
        CryptoPanic POSTs a JSON payload with a 'post' object containing 'title'.
        The headline is injected into ClaudeNewsAnalyst immediately so the next
        analysis cycle uses it — no polling delay.
        """
        try:
            data = request.get_json(force=True, silent=True) or {}
            # CryptoPanic payload: {"post": {"title": "...", "currencies": [...], ...}}
            post  = data.get("post") or data
            title = (post.get("title") or "").strip()
            if title:
                from src.claude_news import analyst as _analyst
                _analyst.push_headline(title)
                return jsonify({"ok": True, "headline": title}), 200
            return jsonify({"ok": False, "reason": "no title"}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

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

        journal_main    = _load_json("data/journal_main.json")   or []
        params_main     = _load_json("data/params_main.json")    or {}
        trend_state     = _load_json("data/trend_state.json")    or {}
        analyst_params  = _load_json("data/analyst_params.json")  or {}
        analyst_history = _load_json("data/analyst_history.json") or []

        def _wl(journal):
            all_closed  = [r for r in journal if r.get("closed")]
            live_closed = [r for r in all_closed if not r.get("dry_run", False)]
            closed = live_closed if live_closed else all_closed
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

        state = get_state()
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
                "total_trades": state.total_trades if state else 0,
                "wins":         state.wins         if state else 0,
                "losses":       (state.total_trades - state.wins) if state else 0,
                "total_pnl":    state.total_pnl    if state else 0,
                "total_fees":   state.total_fees   if state else 0,
                "win_rate":     state.win_rate      if state else 0,
                "best_trade":   state.best_trade    if state else 0,
                "worst_trade":  state.worst_trade   if state else 0,
            },
            "journal_stats":   _wl(journal_main),
            "trend_state":     trend_state,
            "learned_params":  params_main,
            "analyst_params":  analyst_params,
            "analyst_history": analyst_history[-10:],
            "equity_curve":    state.equity_curve if state else [],
            "trade_journal":   journal_main,
        }

        blob = json.dumps(payload, indent=2)
        return Response(
            blob,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=midusbot_export.json"},
        )
