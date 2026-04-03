"""
LLM-based trade analyst with self-improvement.

The analyst runs after every adaptation cycle. It:
  1. Reads recent trade outcomes
  2. Reads its own previous decisions and self-notes
  3. Evaluates whether its last suggestions helped or hurt
  4. Adjusts trading parameters
  5. Updates its own self-notes and analysis strategy for the next run

This creates a feedback loop: the model refines BOTH trading parameters
AND its own reasoning approach over time.

Requires Ollama:
  curl -fsSL https://ollama.com/install.sh | sh
  ollama pull qwen2.5:1.5b
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
TIMEOUT_SEC = 60
DATA_DIR    = Path("data")

# ---------------------------------------------------------------------------
# Parameter schema
# ---------------------------------------------------------------------------

DEFAULT_PARAMS: dict = {
    # Trading parameters (applied immediately to strategy)
    "signal_threshold": 0.0008,
    "min_trend_score":  0.0,
    "skip_assets":      [],
    "prefer_assets":    [],
    "max_secs_in":      240,

    # Self-improvement state (written by the LLM, read by the LLM)
    "self_notes":          "",     # running observations about market behaviour
    "analysis_strategy":   "",     # how the LLM has decided to analyse data
    "wins_at_last_run":    0,
    "trades_at_last_run":  0,

    # Metadata
    "_runs":           0,
    "_last_run_at":    0,
    "_last_reasoning": "",
}

_PARAMS_FILE  = DATA_DIR / "analyst_params.json"
_HISTORY_FILE = DATA_DIR / "analyst_history.json"   # full run log


def load_params() -> dict:
    if _PARAMS_FILE.exists():
        try:
            stored = json.loads(_PARAMS_FILE.read_text())
            return {**DEFAULT_PARAMS, **stored}
        except Exception:
            pass
    return dict(DEFAULT_PARAMS)


def save_params(params: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _PARAMS_FILE.write_text(json.dumps(params, indent=2))


def _append_history(entry: dict) -> None:
    history = []
    if _HISTORY_FILE.exists():
        try:
            history = json.loads(_HISTORY_FILE.read_text())
        except Exception:
            pass
    history.append(entry)
    _HISTORY_FILE.write_text(json.dumps(history[-50:], indent=2))  # keep last 50


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _ollama_available() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _ensure_model() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if any(MODEL in n for n in names):
            return True
        logger.info(f"[ANALYST] Pulling {MODEL}…")
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
# Journal helpers
# ---------------------------------------------------------------------------

def _format_journal(learner: "AdaptiveLearner", n: int = 30) -> list[dict]:
    journal = getattr(learner, "_journal", [])
    closed  = [r for r in journal if r.closed][-n:]
    rows = []
    for r in closed:
        rows.append({
            "asset":     _extract_asset(r.question),
            "side":      r.side,
            "entry":     round(r.entry_price, 4),
            "exit":      round(r.exit_price, 4),
            "pnl":       round(r.pnl_usdc, 4),
            "win":       r.pnl_usdc > 0,
            "momentum":  round(getattr(r, "momentum_signal", 0.0), 6),
            "composite": round(getattr(r, "composite_signal", 0.0), 4),
            "ts":        int(getattr(r, "opened_at", 0)),
        })
    return rows


def _extract_asset(question: str) -> str:
    q = question.upper()
    for k, syms in {
        "BTC":  ["BITCOIN", "BTC"],
        "ETH":  ["ETHEREUM", "ETH"],
        "SOL":  ["SOLANA", "SOL"],
        "DOGE": ["DOGECOIN", "DOGE"],
        "XRP":  ["RIPPLE", "XRP"],
        "BNB":  ["BNB"],
        "HYPE": ["HYPERLIQUID", "HYPE"],
    }.items():
        if any(s in q for s in syms):
            return k
    return "UNKNOWN"


def _asset_stats(rows: list[dict]) -> dict:
    s: dict[str, dict] = {}
    for r in rows:
        a = r["asset"]
        if a not in s:
            s[a] = {"wins": 0, "total": 0, "pnl": 0.0}
        s[a]["total"] += 1
        s[a]["pnl"]   += r["pnl"]
        if r["win"]:
            s[a]["wins"] += 1
    return {
        a: {
            "win_rate": round(v["wins"] / v["total"], 3),
            "trades":   v["total"],
            "total_pnl": round(v["pnl"], 4),
        }
        for a, v in s.items()
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a self-improving trading analyst embedded in a Polymarket bot. You run
repeatedly, and each run you can read your own previous notes and decisions to
improve your analysis over time.

YOUR GOALS (in order):
1. PROTECT CAPITAL — avoid losing trades
2. GROW PROFIT — increase win rate and PnL
3. IMPROVE YOURSELF — update your self_notes and analysis_strategy each run
   based on what you learned. Future runs will read these notes.

THE MARKET:
- 5-minute UpDown crypto markets on Polymarket
- Edge: at window open, YES=0.50 regardless of Binance price. Market makers
  take 60-120s to reprice → oracle lag window.
- Signal: window_return = (price_now - price_at_window_start) / price_at_window_start
- Also tracks: consecutive_window_trend = fraction of last 4 windows same direction

PARAMETERS YOU CONTROL:
- signal_threshold (0.0005–0.005): min |window_return| to trade. Raise to filter noise.
- min_trend_score (0.0–0.75): require consecutive-window agreement. 0.5 = 3/4 windows.
- skip_assets: consistently losing assets to stop trading.
- prefer_assets: proven winners to prioritise.
- max_secs_in (120–240): stop entering after this many seconds into the window.

SELF-IMPROVEMENT FIELDS (you write these for your future self):
- self_notes: running memory of what you've observed about market behaviour,
  which assets behave differently, time-of-day patterns, etc. Append new
  observations; don't erase old ones unless they're proven wrong.
- analysis_strategy: your current approach to interpreting the data. Update
  this when you develop a better analytical method.

OUTPUT FORMAT — respond ONLY with valid JSON, no extra text:
{
  "signal_threshold": <float>,
  "min_trend_score": <float>,
  "skip_assets": [<strings>],
  "prefer_assets": [<strings>],
  "max_secs_in": <int>,
  "reasoning": "<what pattern you found and what you changed>",
  "self_notes": "<updated running memory — append new observations>",
  "analysis_strategy": "<your current analytical approach>"
}"""


def _build_prompt(
    rows: list[dict],
    asset_stats: dict,
    current_params: dict,
    perf_delta: dict,
) -> str:
    total = len(rows)
    wins  = sum(1 for r in rows if r["win"])
    wr    = round(wins / total, 3) if total else 0.0

    parts = []

    # Self-context: what the LLM decided last time and whether it worked
    runs = current_params.get("_runs", 0)
    if runs > 0:
        parts.append(f"=== YOUR PREVIOUS ANALYSIS (Run #{runs}) ===")
        parts.append(f"Last reasoning: {current_params.get('_last_reasoning', 'none')}")
        parts.append(f"Parameters set: threshold={current_params['signal_threshold']:.4f}  "
                     f"min_trend={current_params['min_trend_score']:.2f}  "
                     f"skip={current_params['skip_assets']}  "
                     f"prefer={current_params['prefer_assets']}")
        if perf_delta.get("trades_since") is not None:
            ts = perf_delta["trades_since"]
            ws = perf_delta["wins_since"]
            wr_since = round(ws / ts, 3) if ts else 0.0
            pnl_since = perf_delta.get("pnl_since", 0)
            parts.append(
                f"OUTCOME since your last run: {ts} trades, {ws} wins "
                f"({wr_since:.1%} win rate), PnL ${pnl_since:+.4f}. "
                + ("Your adjustment HELPED." if wr_since >= 0.55 else
                   "Your adjustment DID NOT HELP — reconsider." if ts >= 3 else
                   "Too few trades to evaluate yet.")
            )
        if current_params.get("self_notes"):
            parts.append(f"\nYOUR SELF-NOTES FROM PREVIOUS RUNS:\n{current_params['self_notes']}")
        if current_params.get("analysis_strategy"):
            parts.append(f"\nYOUR CURRENT ANALYSIS STRATEGY:\n{current_params['analysis_strategy']}")
        parts.append("")

    parts.append(f"=== CURRENT PERFORMANCE ===")
    parts.append(f"Last {total} trades: {wins}W / {total-wins}L ({wr:.1%} win rate)")
    parts.append(f"\nPer-asset stats:\n{json.dumps(asset_stats, indent=2)}")
    parts.append(f"\nLast 15 trades (most recent last):\n{json.dumps(rows[-15:], indent=2)}")
    parts.append(f"\nCurrent parameters:\n{json.dumps({k: v for k, v in current_params.items() if not k.startswith('_') and k not in ('self_notes','analysis_strategy','wins_at_last_run','trades_at_last_run')}, indent=2)}")
    parts.append("\nNow produce your updated analysis and self-notes.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Performance delta
# ---------------------------------------------------------------------------

def _perf_delta(rows: list[dict], current_params: dict) -> dict:
    """Win rate / PnL since the last analyst run."""
    last_trades = current_params.get("trades_at_last_run", 0)
    last_wins   = current_params.get("wins_at_last_run", 0)
    total_now   = len(rows)
    wins_now    = sum(1 for r in rows if r["win"])
    trades_since = total_now - last_trades
    wins_since   = wins_now  - last_wins
    pnl_since    = sum(r["pnl"] for r in rows[last_trades:])
    return {
        "trades_since": trades_since,
        "wins_since":   wins_since,
        "pnl_since":    round(pnl_since, 4),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyse_and_update(learner: "AdaptiveLearner") -> dict | None:
    """Run LLM self-improving analysis. Returns new params or None if skipped."""
    if not _ollama_available():
        logger.debug("[ANALYST] Ollama not running — skipping")
        return None
    if not _ensure_model():
        logger.warning(f"[ANALYST] Model {MODEL} unavailable")
        return None

    rows = _format_journal(learner)
    if len(rows) < 5:
        logger.debug("[ANALYST] Need at least 5 closed trades")
        return None

    current_params = load_params()
    stats          = _asset_stats(rows)
    delta          = _perf_delta(rows, current_params)
    prompt         = _build_prompt(rows, stats, current_params, delta)

    try:
        t0   = time.time()
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":   MODEL,
                "system":  _SYSTEM_PROMPT,
                "prompt":  prompt,
                "stream":  False,
                "options": {"temperature": 0.3, "num_predict": 512},
            },
            timeout=TIMEOUT_SEC,
        )
        elapsed = time.time() - t0
        raw = resp.json().get("response", "")
    except Exception as exc:
        logger.warning(f"[ANALYST] LLM call failed: {exc}")
        return None

    try:
        start   = raw.index("{")
        end     = raw.rindex("}") + 1
        payload = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"[ANALYST] Could not parse response: {exc}\nRaw: {raw[:400]}")
        return None

    # Apply trading params (clamped)
    new_params = dict(current_params)
    if "signal_threshold" in payload:
        new_params["signal_threshold"] = float(max(0.0003, min(0.005, payload["signal_threshold"])))
    if "min_trend_score" in payload:
        new_params["min_trend_score"] = float(max(0.0, min(0.75, payload["min_trend_score"])))
    if "skip_assets" in payload and isinstance(payload["skip_assets"], list):
        new_params["skip_assets"] = [str(s).upper() for s in payload["skip_assets"]]
    if "prefer_assets" in payload and isinstance(payload["prefer_assets"], list):
        new_params["prefer_assets"] = [str(s).upper() for s in payload["prefer_assets"]]
    if "max_secs_in" in payload:
        new_params["max_secs_in"] = int(max(120, min(240, payload["max_secs_in"])))

    # Self-improvement: persist the LLM's own notes and strategy
    if payload.get("self_notes"):
        new_params["self_notes"] = str(payload["self_notes"])[:2000]
    if payload.get("analysis_strategy"):
        new_params["analysis_strategy"] = str(payload["analysis_strategy"])[:1000]

    # Metadata
    reasoning = payload.get("reasoning", "")
    new_params["_last_reasoning"]   = reasoning
    new_params["_last_run_at"]      = int(time.time())
    new_params["_runs"]             = new_params.get("_runs", 0) + 1
    new_params["trades_at_last_run"] = len(rows)
    new_params["wins_at_last_run"]   = sum(1 for r in rows if r["win"])

    save_params(new_params)

    # Append to history log
    _append_history({
        "run":        new_params["_runs"],
        "ts":         new_params["_last_run_at"],
        "elapsed_s":  round(elapsed, 1),
        "trades":     len(rows),
        "win_rate":   round(sum(1 for r in rows if r["win"]) / len(rows), 3),
        "params_set": {k: new_params[k] for k in ("signal_threshold","min_trend_score","skip_assets","prefer_assets","max_secs_in")},
        "reasoning":  reasoning,
        "self_notes": new_params.get("self_notes", ""),
        "perf_delta": delta,
    })

    logger.warning(
        f"\n{'='*60}\n"
        f"[ANALYST] Run #{new_params['_runs']} — {elapsed:.1f}s\n"
        f"  threshold : {new_params['signal_threshold']:.4f}\n"
        f"  min_trend : {new_params['min_trend_score']:.2f}\n"
        f"  skip      : {new_params['skip_assets']}\n"
        f"  prefer    : {new_params['prefer_assets']}\n"
        f"  max_secs  : {new_params['max_secs_in']}\n"
        f"  reasoning : {reasoning}\n"
        f"  self_notes: {new_params.get('self_notes','')[:120]}…\n"
        f"{'='*60}"
    )
    return new_params
