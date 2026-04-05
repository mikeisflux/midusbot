"""
Midusbot MCP Server — exposes bot state as tools for Claude Code.

Provides read-only access to:
  - Current bot state (wallet, positions, session stats)
  - Trade journal (recent closed trades)
  - Analyst params (current LLM-recommended thresholds)
  - Alpha decay report
  - Backtest results
  - Feature importance

Run via: python3 -m src.mcp_server
Configured in: .mcp.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure project root is on sys.path exactly once
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

DATA_DIR = Path("data")
JOURNAL_FILE   = DATA_DIR / "journal.jsonl"
PARAMS_FILE    = DATA_DIR / "analyst_params.json"
LEARNER_FILE   = DATA_DIR / "params_main.json"
SESSION_FILE   = DATA_DIR / "session_log.jsonl"
ANALYST_HIST   = DATA_DIR / "analyst_history.json"
CLASSIFIER_FILE = DATA_DIR / "ml_classifier.json"


def _read_jsonl(path: Path, limit: int = 50) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    return [json.loads(l) for l in lines[-limit:] if l.strip()]


def _read_json(path: Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# ── MCP tool handlers ────────────────────────────────────────────────────────

def tool_bot_state() -> dict:
    """Return current analyst params, learner params, and journal summary."""
    analyst = _read_json(PARAMS_FILE)
    learner = _read_json(LEARNER_FILE)
    trades  = _read_jsonl(JOURNAL_FILE, limit=200)

    closed = [t for t in trades if t.get("outcome") in ("win", "loss")]
    wins   = sum(1 for t in closed if t.get("outcome") == "win")
    pnl    = sum(t.get("pnl_usdc", 0.0) for t in closed)

    return {
        "analyst_params": analyst,
        "learner_params": learner,
        "journal_summary": {
            "total_trades": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / len(closed), 3) if closed else 0.0,
            "total_pnl_usdc": round(pnl, 2),
        },
    }


def tool_recent_trades(limit: int = 20) -> list[dict]:
    """Return most recent closed trades from journal."""
    return _read_jsonl(JOURNAL_FILE, limit=limit)


def tool_session_stats() -> dict:
    """Return per-asset signal accuracy from session log."""
    rows = _read_jsonl(SESSION_FILE, limit=500)
    stats: dict[str, dict] = {}
    for r in rows:
        sym = r.get("symbol", "UNKNOWN")
        if sym not in stats:
            stats[sym] = {"correct": 0, "total": 0}
        if r.get("predicted") and r.get("actual"):
            stats[sym]["total"] += 1
            if r["predicted"] == r["actual"]:
                stats[sym]["correct"] += 1
    for sym, s in stats.items():
        s["accuracy"] = round(s["correct"] / s["total"], 3) if s["total"] else 0.0
    return stats


def tool_analyst_history(limit: int = 5) -> list[dict]:
    """Return recent analyst LLM runs."""
    hist = _read_json(ANALYST_HIST)
    if isinstance(hist, list):
        return hist[-limit:]
    return []


def tool_alpha_decay() -> dict:
    """Return alpha decay analysis from learner."""
    try:
        from src.learner import Learner
        learner = Learner()
        return {"report": learner.alpha_decay_report()}
    except Exception as e:
        return {"error": str(e)}


def tool_feature_importance() -> dict:
    """Return signal feature importance from learner."""
    try:
        from src.learner import Learner
        learner = Learner()
        return {"report": learner.feature_importance()}
    except Exception as e:
        return {"error": str(e)}


def tool_backtest(lookback_days: int = 7) -> dict:
    """Run backtest engine against session_log."""
    try:
        from src.backtest import BacktestEngine
        engine = BacktestEngine()
        results = engine.run(lookback_days=lookback_days)
        return {
            "total_trades": results.total_trades,
            "win_rate": round(results.win_rate, 3),
            "total_pnl": round(results.total_pnl, 2),
            "per_asset": results.per_asset,
        }
    except Exception as e:
        return {"error": str(e)}


# ── MCP stdio protocol ───────────────────────────────────────────────────────

TOOLS = {
    "bot_state": {
        "description": "Get current bot state: analyst params, learner params, journal summary",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda args: tool_bot_state(),
    },
    "recent_trades": {
        "description": "Get recent closed trades from journal",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
            "required": [],
        },
        "handler": lambda args: tool_recent_trades(args.get("limit", 20)),
    },
    "session_stats": {
        "description": "Get per-asset signal accuracy from session log",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda args: tool_session_stats(),
    },
    "analyst_history": {
        "description": "Get recent LLM analyst run history",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 5}},
            "required": [],
        },
        "handler": lambda args: tool_analyst_history(args.get("limit", 5)),
    },
    "alpha_decay": {
        "description": "Get alpha decay report — is edge shrinking over time?",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda args: tool_alpha_decay(),
    },
    "feature_importance": {
        "description": "Get feature importance — which signals actually predict outcomes?",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda args: tool_feature_importance(),
    },
    "backtest": {
        "description": "Run backtest engine against historical session data",
        "inputSchema": {
            "type": "object",
            "properties": {"lookback_days": {"type": "integer", "default": 7}},
            "required": [],
        },
        "handler": lambda args: tool_backtest(args.get("lookback_days", 7)),
    },
}


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle(req: dict) -> dict:
    method = req.get("method", "")
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "midusbot", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        tools_list = [
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            }
            for name, spec in TOOLS.items()
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}

    if method == "tools/call":
        params  = req.get("params", {})
        name    = params.get("name", "")
        args    = params.get("arguments", {})
        if name not in TOOLS:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        try:
            result = TOOLS[name]["handler"](args)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": False,
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    # notifications/initialized and other notifications
    return None  # type: ignore[return-value]


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(req)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
