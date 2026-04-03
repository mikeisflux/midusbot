"""
LLM-based trade analyst — uses a local Ollama model to reason over the
trade journal and suggest parameter improvements.

Requires Ollama running locally with a small model:
  curl -fsSL https://ollama.com/install.sh | sh
  ollama pull qwen2.5:1.5b

If Ollama is not available, analysis is silently skipped.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from loguru import logger

if TYPE_CHECKING:
    from src.learner import AdaptiveLearner

OLLAMA_URL  = "http://localhost:11434"
MODEL       = "qwen2.5:1.5b"
TIMEOUT_SEC = 45
DATA_DIR    = Path("data")


# ---------------------------------------------------------------------------
# Parameter schema the LLM can tune
# ---------------------------------------------------------------------------

DEFAULT_PARAMS: dict = {
    "signal_threshold":   0.0008,   # minimum |window_return| to trade
    "min_trend_score":    0.0,      # minimum |consecutive_window_trend| to trade (0 = off)
    "skip_assets":        [],       # asset symbols to avoid (e.g. ["HYPE"])
    "prefer_assets":      [],       # assets to prefer when signal is close
    "max_secs_in":        240,      # timing window upper bound
}

_PARAMS_FILE = DATA_DIR / "analyst_params.json"


def load_params() -> dict:
    """Load analyst params from disk, falling back to defaults."""
    if _PARAMS_FILE.exists():
        try:
            stored = json.loads(_PARAMS_FILE.read_text())
            merged = {**DEFAULT_PARAMS, **stored}
            return merged
        except Exception:
            pass
    return dict(DEFAULT_PARAMS)


def save_params(params: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _PARAMS_FILE.write_text(json.dumps(params, indent=2))


# ---------------------------------------------------------------------------
# Ollama health check
# ---------------------------------------------------------------------------

def _ollama_available() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _ensure_model() -> bool:
    """Return True if the model is available (pulls if needed)."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        tags = r.json().get("models", [])
        names = [m.get("name", "") for m in tags]
        if any(MODEL in n for n in names):
            return True
        # Model not present — pull it (takes a while on first run)
        logger.info(f"[ANALYST] Pulling {MODEL} — this takes a few minutes on first run…")
        resp = requests.post(
            f"{OLLAMA_URL}/api/pull",
            json={"name": MODEL, "stream": False},
            timeout=300,
        )
        return resp.status_code == 200
    except Exception as exc:
        logger.warning(f"[ANALYST] Model check failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Journal formatting
# ---------------------------------------------------------------------------

def _format_journal(learner: "AdaptiveLearner", n: int = 20) -> list[dict]:
    journal = getattr(learner, "_journal", [])
    closed  = [r for r in journal if r.closed][-n:]
    rows = []
    for r in closed:
        rows.append({
            "asset":      _extract_asset(r.question),
            "side":       r.side,
            "entry":      round(r.entry_price, 4),
            "exit":       round(r.exit_price, 4),
            "pnl":        round(r.pnl_usdc, 4),
            "win":        r.pnl_usdc > 0,
            "momentum":   round(getattr(r, "momentum_signal", 0.0), 6),
            "composite":  round(getattr(r, "composite_signal", 0.0), 4),
            "ts":         int(getattr(r, "opened_at", 0)),
        })
    return rows


def _extract_asset(question: str) -> str:
    q = question.upper()
    for sym in ["BITCOIN", "BTC"]:
        if sym in q:
            return "BTC"
    for sym in ("ETHEREUM", "ETH"):
        if sym in q:
            return "ETH"
    for sym in ("SOLANA", "SOL"):
        if sym in q:
            return "SOL"
    for sym in ("DOGECOIN", "DOGE"):
        if sym in q:
            return "DOGE"
    for sym in ("RIPPLE", "XRP"):
        if sym in q:
            return "XRP"
    for sym in ("BNB",):
        if sym in q:
            return "BNB"
    for sym in ("HYPERLIQUID", "HYPE"):
        if sym in q:
            return "HYPE"
    return "UNKNOWN"


def _asset_win_rates(rows: list[dict]) -> dict:
    stats: dict[str, dict] = {}
    for r in rows:
        a = r["asset"]
        if a not in stats:
            stats[a] = {"wins": 0, "total": 0}
        stats[a]["total"] += 1
        if r["win"]:
            stats[a]["wins"] += 1
    return {
        a: {
            "win_rate": round(v["wins"] / v["total"], 3),
            "trades": v["total"],
        }
        for a, v in stats.items()
    }


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a profit-focused trading analyst for a Polymarket bot
trading 5-minute UpDown crypto markets. Your PRIMARY goal is to MAKE MONEY and
PROTECT CAPITAL. Winning 60%+ of trades is profitable. A trade that should not
be taken is worth more than a marginal trade that loses. Be ruthless: if an
asset is consistently losing, eliminate it. If the signal threshold is too low,
raise it. Fewer, higher-quality trades beat more, lower-quality trades.

The bot's edge: at window open YES=0.50 regardless of Binance price. Market
makers take 60-120s to reprice. Signal = window_return = (current - start) / start.
The bot also tracks consecutive-window trend (have the last 3-4 windows been
going the same direction?). Both signals together = higher confidence.

Parameters you control:
- signal_threshold: minimum |window_return| to trade. Raise it if losing on
  weak signals. Range: 0.0005–0.005.
- min_trend_score: require consecutive-window trend agreement. 0.5 means 3/4
  recent windows must agree. 0.0 = off. Range: 0.0–0.75.
- skip_assets: list of CONSISTENTLY LOSING assets to stop trading entirely.
- prefer_assets: assets with PROVEN win rates — prioritize these.
- max_secs_in: stop entering after this many seconds. Reduce if late entries
  lose more than early ones. Range: 120–240.

Respond ONLY with valid JSON in exactly this format (no extra text):
{
  "signal_threshold": <float>,
  "min_trend_score": <float>,
  "skip_assets": [<strings>],
  "prefer_assets": [<strings>],
  "max_secs_in": <int>,
  "reasoning": "<1-2 sentences: what losing pattern you identified and what you changed>"
}"""


def _build_prompt(rows: list[dict], asset_stats: dict, current_params: dict) -> str:
    total  = len(rows)
    wins   = sum(1 for r in rows if r["win"])
    win_rt = round(wins / total, 3) if total else 0.0
    return (
        f"Recent {total} trades: {wins} wins / {total - wins} losses ({win_rt:.1%} win rate)\n\n"
        f"Per-asset stats: {json.dumps(asset_stats)}\n\n"
        f"Last 10 trades (most recent last):\n{json.dumps(rows[-10:], indent=2)}\n\n"
        f"Current parameters: {json.dumps(current_params)}\n\n"
        f"Analyze the data and suggest parameter adjustments. "
        f"Focus on patterns: which assets win/lose, what momentum values predict wins, "
        f"whether requiring stronger trends would help filter bad signals."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyse_and_update(learner: "AdaptiveLearner") -> dict | None:
    """
    Run LLM analysis on the recent journal and update analyst_params.json.
    Returns the new params dict, or None if analysis was skipped.
    """
    if not _ollama_available():
        logger.debug("[ANALYST] Ollama not available — skipping analysis")
        return None

    if not _ensure_model():
        logger.warning(f"[ANALYST] Model {MODEL} not available")
        return None

    rows        = _format_journal(learner)
    if len(rows) < 5:
        logger.debug("[ANALYST] Not enough closed trades yet for analysis")
        return None

    asset_stats    = _asset_win_rates(rows)
    current_params = load_params()
    prompt         = _build_prompt(rows, asset_stats, current_params)

    try:
        t0 = time.time()
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":  MODEL,
                "system": _SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 256},
            },
            timeout=TIMEOUT_SEC,
        )
        elapsed = time.time() - t0
        raw = resp.json().get("response", "")
    except Exception as exc:
        logger.warning(f"[ANALYST] LLM call failed: {exc}")
        return None

    # Extract JSON from response (model sometimes wraps it in markdown)
    try:
        start = raw.index("{")
        end   = raw.rindex("}") + 1
        payload = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"[ANALYST] Could not parse LLM response: {exc}\nRaw: {raw[:300]}")
        return None

    # Sanitize and clamp values
    new_params: dict = dict(current_params)
    if "signal_threshold" in payload:
        new_params["signal_threshold"] = float(
            max(0.0003, min(0.005, payload["signal_threshold"]))
        )
    if "min_trend_score" in payload:
        new_params["min_trend_score"] = float(
            max(0.0, min(0.75, payload["min_trend_score"]))
        )
    if "skip_assets" in payload and isinstance(payload["skip_assets"], list):
        new_params["skip_assets"] = [str(s).upper() for s in payload["skip_assets"]]
    if "prefer_assets" in payload and isinstance(payload["prefer_assets"], list):
        new_params["prefer_assets"] = [str(s).upper() for s in payload["prefer_assets"]]
    if "max_secs_in" in payload:
        new_params["max_secs_in"] = int(max(120, min(240, payload["max_secs_in"])))

    reasoning = payload.get("reasoning", "")

    # Store reasoning and run metadata in the params file for export/tracking
    new_params["_last_run_at"]  = int(time.time())
    new_params["_last_reasoning"] = reasoning
    new_params["_runs"] = new_params.get("_runs", 0) + 1
    save_params(new_params)

    logger.warning(
        f"\n{'='*60}\n"
        f"[ANALYST] Run #{new_params['_runs']} — {elapsed:.1f}s\n"
        f"  threshold : {new_params['signal_threshold']:.4f}\n"
        f"  min_trend : {new_params['min_trend_score']:.2f}\n"
        f"  skip      : {new_params['skip_assets']}\n"
        f"  prefer    : {new_params['prefer_assets']}\n"
        f"  max_secs  : {new_params['max_secs_in']}\n"
        f"  reasoning : {reasoning}\n"
        f"{'='*60}"
    )
    return new_params
