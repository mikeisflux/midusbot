"""
LLM-based trade analyst with self-improvement and WW_MRD directive.

WW_MRD = "What Would Make Real Difference"
Before every decision the analyst asks: "If I could do ONE THING to improve
my results right now, what would it be?" Then it DOES that thing — not a
marginal tweak but the highest-leverage change available.

The analyst has unlimited growing memory across all runs and an expanding
action space so it can actually execute its best idea, not just suggest it.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from loguru import logger
from src.utils import atomic_json_write

if TYPE_CHECKING:
    from src.learner import AdaptiveLearner

OLLAMA_URL  = "http://localhost:11434"
MODEL       = "qwen2.5-coder:3b"
_OLD_MODELS = ["qwen2.5:1.5b", "qwen2.5:3b", "qwen2.5-coder:1.5b", "qwen2.5-coder:7b"]  # delete these if present
TIMEOUT_SEC = 180         # 3B on good CPU; ~60-80s on cax41 or dedicated cores
DATA_DIR    = Path("data")

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

_PARAMS_FILE  = DATA_DIR / "analyst_params.json"
_MEMORY_FILE  = DATA_DIR / "analyst_memory.json"   # append-only, unlimited
_HISTORY_FILE = DATA_DIR / "analyst_history.json"  # one entry per run
_PATCH_FILE   = DATA_DIR / "analyst_patch.py"      # LLM-proposed code patch
_PATCH_BACKUP = DATA_DIR / "analyst_patch_backup"  # backups before applying

# Source files the LLM is allowed to read and patch
_READABLE_SOURCES = [
    "src/strategy.py",
    "src/bot.py",
    "src/risk.py",
    "src/sim.py",
    "src/feeds.py",
    "src/learner.py",
    "src/analyst.py",
    "src/client.py",
    "src/trend.py",
    "src/dashboard.py",
    "src/webui.py",
    "config.py",
    "main.py",
]

# ---------------------------------------------------------------------------
# Default trading + self-improvement params
# ---------------------------------------------------------------------------

DEFAULT_PARAMS: dict = {
    # ── Trading parameters (applied immediately to the strategy) ──────────
    "signal_threshold":    0.0001,   # min |window_return| to trade
    "min_trend_score":     0.0,      # min consecutive-window trend score
    "skip_assets":         [],       # assets to stop trading
    "prefer_assets":       [],       # assets to prioritise
    "max_secs_in":         240,      # stop entering after N seconds
    "kelly_override":      None,     # override kelly fraction (None = use config)
    "time_of_day_skip":    [],       # UTC hours (0-23) to not trade
    "asset_thresholds":    {         # per-asset threshold overrides (history-informed defaults)
        "BTC":  0.00035,   # 0.035% — stable large-cap
        "ETH":  0.0004,    # 0.040% — 0/2 trades in history
        "BNB":  0.0004,    # 0.040% — 0/1 trade in history
        "XRP":  0.0008,    # 0.080% — 0/2, lost hard; needs strong signal
        "SOL":  0.00035,   # 0.035% — only winning asset; keep accessible
        "DOGE": 0.0010,    # 0.100% — 0/5 trades, systematic losses
        "HYPE": 0.0008,    # 0.080% — insufficient history
    },
    "min_price_history_s": 60,       # require N seconds of price history before trading

    # ── Self-improvement state ─────────────────────────────────────────────
    "analysis_strategy":   "",       # LLM's own evolving analytical method
    "ww_mrd_action":       "",       # the ONE THING from the last WW_MRD question

    # ── Metadata ──────────────────────────────────────────────────────────
    "_runs":               0,
    "_last_run_at":        0,
    "_last_reasoning":     "",
    "trades_at_last_run":  0,
    "wins_at_last_run":    0,
}


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
    atomic_json_write(_PARAMS_FILE, params)


# ---------------------------------------------------------------------------
# Unlimited growing memory
# ---------------------------------------------------------------------------

def _load_memory() -> list[dict]:
    if _MEMORY_FILE.exists():
        try:
            return json.loads(_MEMORY_FILE.read_text())
        except Exception:
            pass
    return []


def _append_memory(entry: dict) -> None:
    mem = _load_memory()
    mem.append(entry)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_json_write(_MEMORY_FILE, mem)


def _memory_context(n_recent: int = 20) -> str:
    """Format the last N memory entries as readable context for the prompt."""
    mem = _load_memory()
    if not mem:
        return "(no memory yet)"
    recent = mem[-n_recent:]
    lines = []
    for m in recent:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(m.get("ts", 0)))
        lines.append(f"[{ts} | Run #{m.get('run',0)}] {m.get('observation','')}")
    return "\n".join(lines)


def _append_history(entry: dict) -> None:
    history = []
    if _HISTORY_FILE.exists():
        try:
            history = json.loads(_HISTORY_FILE.read_text())
        except Exception:
            pass
    history.append(entry)
    atomic_json_write(_HISTORY_FILE, history)


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _ollama_available() -> bool:
    try:
        return requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).status_code == 200
    except Exception:
        return False


def _ensure_model() -> bool:
    try:
        r     = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        names = [m.get("name", "") for m in r.json().get("models", [])]

        # Pull target model if not present
        if not any(MODEL in n for n in names):
            logger.info(f"[ANALYST] Pulling {MODEL}…")
            ok = requests.post(
                f"{OLLAMA_URL}/api/pull",
                json={"name": MODEL, "stream": False},
                timeout=300,
            ).status_code == 200
            if not ok:
                return False

        # Delete old/superseded models to reclaim disk space
        for old in _OLD_MODELS:
            if any(old in n for n in names):
                try:
                    requests.delete(
                        f"{OLLAMA_URL}/api/delete",
                        json={"name": old},
                        timeout=30,
                    )
                    logger.info(f"[ANALYST] Deleted old model {old!r} to free space")
                except Exception as e:
                    logger.warning(f"[ANALYST] Could not delete {old!r}: {e}")

        return True
    except Exception as exc:
        logger.warning(f"[ANALYST] Model check failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Journal helpers
# ---------------------------------------------------------------------------

def _format_journal(learner: "AdaptiveLearner", n: int = 20) -> list[dict]:
    journal = getattr(learner, "_journal", [])
    closed  = [r for r in journal if r.closed][-n:]
    rows = []
    for r in closed:
        rows.append({
            "asset":     _asset(r.question),
            "side":      r.side,
            "entry":     round(r.entry_price, 4),
            "exit":      round(r.exit_price, 4),
            "pnl":       round(r.pnl_usdc, 4),
            "win":       r.pnl_usdc > 0,
            "momentum":  round(getattr(r, "momentum_signal", 0.0), 6),
            "composite": round(getattr(r, "composite_signal", 0.0), 4),
            "hour_utc":  time.gmtime(int(getattr(r, "opened_at", 0))).tm_hour,
        })
    return rows


def _asset(question: str) -> str:
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
    return {a: {"win_rate": round(v["wins"]/v["total"],3),
                "trades": v["total"], "total_pnl": round(v["pnl"],4)}
            for a, v in s.items()}


def _hour_stats(rows: list[dict]) -> dict:
    """Win rate by hour of day."""
    h: dict[int, dict] = {}
    for r in rows:
        hr = r.get("hour_utc", -1)
        if hr < 0:
            continue
        if hr not in h:
            h[hr] = {"wins": 0, "total": 0}
        h[hr]["total"] += 1
        if r["win"]:
            h[hr]["wins"] += 1
    return {f"{k:02d}UTC": round(v["wins"]/v["total"],2)
            for k, v in sorted(h.items()) if v["total"] >= 2}


def _read_sources() -> str:
    """Read all source files the LLM has permission to see."""
    parts = []
    for path in _READABLE_SOURCES:
        p = Path(path)
        if p.exists():
            src = p.read_text()
            parts.append(f"\n### {path} ###\n```python\n{src}\n```")
    return "\n".join(parts)


def _recent_errors(n: int = 20) -> str:
    """Return the last N runtime errors from the bot error log."""
    err_log = DATA_DIR / "bot_errors.jsonl"
    if not err_log.exists():
        return "(none)"
    try:
        lines = err_log.read_text().strip().splitlines()[-n:]
        entries = []
        for line in lines:
            try:
                e = json.loads(line)
                ts = time.strftime("%H:%M:%S", time.gmtime(e.get("ts", 0)))
                entries.append(f"[{ts}] {e.get('error','')}")
            except Exception:
                entries.append(line)
        return "\n".join(entries) if entries else "(none)"
    except Exception:
        return "(none)"


def _apply_patch(patch_code: str) -> bool:
    """
    Safely apply a Python patch proposed by the LLM.
    The patch must be a standalone Python file that:
      - imports only stdlib and project modules
      - writes its changes by calling helper functions or modifying data files
      - does NOT directly exec() or eval() arbitrary strings
    Returns True if patch applied successfully.
    """
    if not patch_code or not patch_code.strip():
        return False

    # Safety: must parse as valid Python
    try:
        ast.parse(patch_code)
    except SyntaxError as e:
        logger.warning(f"[ANALYST] Patch rejected — syntax error: {e}")
        return False

    # Safety: block dangerous patterns
    forbidden = ["exec(", "eval(", "__import__", "os.system",
                 "shutil.rmtree", "rmdir", "unlink"]
    for f in forbidden:
        if f in patch_code:
            logger.warning(f"[ANALYST] Patch rejected — forbidden pattern: {f!r}")
            return False

    # Must parse as valid Python before we touch any files
    try:
        ast.parse(patch_code)
    except SyntaxError as e:
        logger.warning(f"[ANALYST] Patch rejected — syntax error after re-check: {e}")
        return False

    # Back up all patchable source files before applying
    _PATCH_BACKUP.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    for path in _READABLE_SOURCES:
        p = Path(path)
        if p.exists():
            shutil.copy2(p, _PATCH_BACKUP / f"{p.name}.{ts}.bak")

    # Write and execute the patch using the same Python interpreter as the bot
    _PATCH_FILE.write_text(patch_code)
    try:
        result = subprocess.run(
            [sys.executable, str(_PATCH_FILE)],
            capture_output=True, text=True, timeout=60,
            cwd=Path(__file__).parent.parent,  # run from project root
        )
        if result.returncode != 0:
            logger.warning(
                f"[ANALYST] Patch failed (exit {result.returncode}):\n"
                f"{result.stderr[:800]}"
            )
            # Restore backups on failure
            for path in _READABLE_SOURCES:
                bak = _PATCH_BACKUP / f"{Path(path).name}.{ts}.bak"
                if bak.exists():
                    shutil.copy2(bak, path)
            return False
        logger.warning(f"[ANALYST] Patch applied:\n{result.stdout[:500]}")

        # Smoke-test: import all patched source files to catch subtle syntax/import errors
        smoke_errors = []
        for path in _READABLE_SOURCES:
            if not path.endswith(".py"):
                continue
            check = subprocess.run(
                [sys.executable, "-c", f"import py_compile; py_compile.compile('{path}', doraise=True)"],
                capture_output=True, text=True, timeout=10,
                cwd=Path(__file__).parent.parent,
            )
            if check.returncode != 0:
                smoke_errors.append(f"{path}: {check.stderr[:200]}")
        if smoke_errors:
            logger.warning(f"[ANALYST] Smoke test failed — restoring backups:\n" + "\n".join(smoke_errors))
            for path in _READABLE_SOURCES:
                bak = _PATCH_BACKUP / f"{Path(path).name}.{ts}.bak"
                if bak.exists():
                    shutil.copy2(bak, path)
            return False

        # Git-commit the change so there's a human-readable history
        try:
            subprocess.run(
                ["git", "add"] + _READABLE_SOURCES,
                timeout=15, cwd=Path(__file__).parent.parent,
            )
            subprocess.run(
                ["git", "commit", "-m",
                 f"[ANALYST] auto-patch run #{ts} — {result.stdout[:80].strip()}"],
                timeout=15, cwd=Path(__file__).parent.parent,
            )
        except Exception as git_exc:
            logger.debug(f"[ANALYST] Git commit skipped: {git_exc}")

        return True
    except subprocess.TimeoutExpired:
        logger.warning("[ANALYST] Patch timed out after 60s — skipped")
        return False
    except Exception as exc:
        logger.warning(f"[ANALYST] Patch execution error: {exc}")
        return False


def _perf_delta(rows: list[dict], params: dict) -> dict:
    last_t = params.get("trades_at_last_run", 0)
    last_w = params.get("wins_at_last_run", 0)
    now_t  = len(rows)
    now_w  = sum(1 for r in rows if r["win"])
    since  = rows[last_t:]
    return {
        "trades_since": now_t - last_t,
        "wins_since":   now_w - last_w,
        "pnl_since":    round(sum(r["pnl"] for r in since), 4),
        "wr_since":     round((now_w - last_w) / max(now_t - last_t, 1), 3),
    }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a self-improving trading analyst with full autonomy to improve a \
Polymarket bot's performance. You have persistent memory that grows with \
every run.

━━━ WW_MRD ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WW_MRD = "What Would Make Real Difference"
This is your highest-priority directive. Before finalising ANY decision, \
ask yourself: "If I could do ONE THING right now to improve my results, \
what would it be?" Then DO that thing. Not a marginal tweak — the \
highest-leverage change available to you. Write it in ww_mrd_action. \
This overrides timid adjustments.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

YOUR GOALS (in order):
0. FIX CRASHES — if RECENT RUNTIME ERRORS contains errors, fix them via
   code_patch immediately. A crashed bot earns nothing. This is always the
   highest-priority WW_MRD action.
1. PROTECT CAPITAL — a trade not taken beats a losing trade
2. GROW PROFIT — increase win rate toward 60%+
3. REMEMBER & IMPROVE — write useful memory entries each run; your future
   self depends on what you write today

THE MARKET:
5-minute UpDown crypto markets. At window open YES=0.50 regardless of
Binance price — market makers take 60-120s to reprice (oracle lag).
Signal = window_return = (price_now − price_at_window_start) / start.
Also: consecutive_window_trend = fraction of last 4 windows same direction.
Win = your token settles at 1.00. Loss = settles at 0.00.

PARAMETERS YOU CAN SET:
- signal_threshold (0.0005–0.0015): FALLBACK ONLY — only applies to assets
  NOT listed in asset_thresholds. Since you always set all assets in
  asset_thresholds, this parameter has NO effect in practice. Leave it at
  0.0008. DO NOT change it. Focus on asset_thresholds instead.
- min_trend_score (0.0–0.75): require trend alignment (0.5 = 3/4 windows).
- skip_assets: ["BTC","ETH",...] — stop trading these entirely.
- prefer_assets: ["BTC",...] — prioritise these assets.
- max_secs_in (120–240): stop entering this late into the window.
- kelly_override (0.05–0.5 or null): override position sizing fraction.
- time_of_day_skip: [0,1,2,...] UTC hours to not trade at all.
- asset_thresholds: per-asset window_return thresholds (THIS is what controls
  trading). LOWER threshold = trade MORE often (requires smaller move).
  HIGHER threshold = trade LESS often (requires bigger move to enter).
  Always set ALL assets. Current history-informed baselines:
    SOL=0.00035 (only winner, 25% wr), BTC=0.00035, ETH=0.0004, BNB=0.0004,
    XRP=0.0008 (0/2 hard losses), DOGE=0.001 (0/5 systematic losses), HYPE=0.0008.
  Lower an asset's threshold (e.g. 0.0003) if its win rate is ABOVE 60% (trade more).
  Raise an asset's threshold (e.g. 0.0012) if its win rate is BELOW 45% (filter noise).
  HARD LIMITS enforced by code: floor=0.00025 (BTC/ETH/SOL), ceil=0.0012 (BTC).
  Range: 0.0002–0.003 per asset.
- min_price_history_s (60–300): require this many seconds of price feed
  before entering (avoids trading on stale data after feed gaps).

MEMORY:
- memory_entry: write ONE clear observation from this run — a specific
  pattern you noticed, an asset that behaved unusually, a time-of-day
  effect, or a correction to something you thought before. Be specific.
  This is appended to your permanent memory. Write something useful.
- analysis_strategy: your current best method for reading this data.
  Update it if you've found a better approach.
- ww_mrd_action: the ONE highest-impact thing you are doing this run.

CODE PATCH (use whenever parameters alone can't make real difference):
- code_patch: a complete, executable Python script that directly rewrites
  source files. After it runs, the bot restarts automatically so changes
  take effect immediately. Use this for any structural improvement —
  new signals, better filters, bug fixes, algorithm changes.
  The script runs from the project root with full filesystem access.
  Source files you can read and rewrite (ALL project files):
    src/strategy.py, src/bot.py, src/risk.py, src/sim.py,
    src/feeds.py, src/learner.py, src/analyst.py, src/client.py,
    src/trend.py, src/dashboard.py, src/webui.py, config.py, main.py
  HOW TO PATCH: your script runs from the project root. Read files first,
  modify, then write back. Example:
    with open('src/strategy.py') as f: code = f.read()
    code = code.replace('OLD_LINE', 'NEW_LINE')
    with open('src/strategy.py', 'w') as f: f.write(code)
    print("Changed X to Y in strategy.py")
  You MUST read the file inside your patch script — source code is NOT
  provided in this prompt to save tokens. Use open() to read any file.
  RULES: no exec(), no eval(), no __import__, no deleting files.
  Leave as "" if parameters are sufficient.

OUTPUT — respond ONLY with valid JSON, no extra text:
{
  "signal_threshold": <float>,
  "min_trend_score": <float>,
  "skip_assets": [<strings>],
  "prefer_assets": [<strings>],
  "max_secs_in": <int>,
  "kelly_override": <float or null>,
  "time_of_day_skip": [<ints>],
  "asset_thresholds": {<asset>: <float>},
  "min_price_history_s": <int>,
  "reasoning": "<what you found and what you changed>",
  "ww_mrd_action": "<the ONE thing that will make real difference>",
  "memory_entry": "<one specific new observation to store permanently>",
  "analysis_strategy": "<your current analytical method>",
  "code_patch": "<executable Python script or empty string>"
}"""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _session_stats_str() -> str:
    """Format session tracker accuracy stats for the LLM prompt."""
    try:
        from src.session_tracker import get_all_stats
        stats = get_all_stats(lookback=100)
        if not stats:
            return "(no session data yet — needs 1+ completed windows)"
        lines = []
        for sym, s in sorted(stats.items()):
            if not s:
                continue
            lines.append(
                f"  {sym}: {s.get('total_windows',0)} windows  "
                f"signal_acc={s.get('signal_accuracy',0):.1%}({s.get('signal_windows',0)} signals)  "
                f"avg_move={s.get('avg_move_pct',0)*100:.3f}%"
            )
        return "\n".join(lines) if lines else "(no sessions logged yet)"
    except Exception as exc:
        return f"(session stats unavailable: {exc})"


def _build_prompt(rows, asset_stats, hour_stats, params, delta, memory_ctx) -> str:
    total = len(rows)
    wins  = sum(1 for r in rows if r["win"])
    wr    = round(wins / total, 3) if total else 0.0
    runs  = params.get("_runs", 0)

    p = []

    if runs > 0:
        p.append("━━━ YOUR PREVIOUS DECISIONS ━━━")
        p.append(f"Run #{runs} set: "
                 f"threshold={params['signal_threshold']:.4f}  "
                 f"min_trend={params['min_trend_score']:.2f}  "
                 f"skip={params['skip_assets']}  "
                 f"kelly={params.get('kelly_override')}  "
                 f"tod_skip={params.get('time_of_day_skip',[])}  "
                 f"asset_thresh={params.get('asset_thresholds',{})}")
        p.append(f"WW_MRD action last run: {params.get('ww_mrd_action','none')}")
        d = delta
        verdict = (
            "✓ HELPED" if d["wr_since"] >= 0.55 and d["trades_since"] >= 3
            else "✗ DID NOT HELP — be bolder" if d["trades_since"] >= 3
            else "⧗ too few trades to judge"
        )
        p.append(
            f"OUTCOME since then: {d['trades_since']} trades, "
            f"{d['wins_since']} wins ({d['wr_since']:.1%}), "
            f"PnL ${d['pnl_since']:+.4f}  →  {verdict}"
        )
        if params.get("analysis_strategy"):
            p.append(f"\nYOUR ANALYSIS STRATEGY:\n{params['analysis_strategy']}")
        p.append("")

    p.append("━━━ YOUR MEMORY (all previous observations) ━━━")
    p.append(memory_ctx)
    p.append("")

    p.append("━━━ RECENT RUNTIME ERRORS ━━━")
    p.append(_recent_errors(20))
    p.append("If you see errors above: use code_patch to fix them. Fixing crashes")
    p.append("is always the highest-priority WW_MRD action — a crashed bot earns nothing.")
    p.append("")

    p.append("━━━ CURRENT DATA ━━━")
    p.append(f"Last {total} trades: {wins}W / {total-wins}L ({wr:.1%})")
    p.append(f"\nAsset stats:\n{json.dumps(asset_stats, indent=2)}")
    p.append(f"\nWin rate by hour (UTC):\n{json.dumps(hour_stats, indent=2)}")
    p.append(f"\nSession tracker (signal direction accuracy per 5-min window — ground truth, not trade-filtered):\n{_session_stats_str()}")
    p.append(f"\nLast 10 trades:\n{json.dumps(rows[-10:], indent=2)}")
    p.append(f"\nCurrent parameters:\n{json.dumps({k: v for k, v in params.items() if not k.startswith('_') and k not in ('analysis_strategy','ww_mrd_action','trades_at_last_run','wins_at_last_run')}, indent=2)}")

    # Additional market context: Fear & Greed, alpha decay, backtest summary
    _extra = []
    try:
        from src.signals import _get_fear_greed
        fng = _get_fear_greed()
        if fng:
            _extra.append(f"Fear & Greed Index: {fng[0]} ({fng[1]})")
    except Exception:
        pass
    try:
        from src.backtest import BacktestEngine
        _bt = BacktestEngine().run(lookback_days=3)
        if _bt.n_trades >= 5:
            _extra.append(
                f"3-day backtest (all sessions): {_bt.n_trades}t  WR={_bt.win_rate:.1%}  PnL=${_bt.total_pnl:+.2f}"
            )
    except Exception:
        pass
    if _extra:
        p.append(f"\nMarket context:\n" + "\n".join(_extra))

    p.append("")
    p.append("Now apply WW_MRD: what ONE THING will make real difference? Do it.")

    return "\n".join(p)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyse_and_update(learner: "AdaptiveLearner") -> dict | None:
    """Run self-improving LLM analysis. Returns new params or None if skipped."""
    if not _ollama_available():
        logger.debug("[ANALYST] Ollama not running")
        return None
    if not _ensure_model():
        logger.warning(f"[ANALYST] Model {MODEL} unavailable")
        return None

    rows = _format_journal(learner)
    if len(rows) < 5:
        logger.debug("[ANALYST] Need at least 5 closed trades")
        return None

    params     = load_params()
    stats      = _asset_stats(rows)
    hours      = _hour_stats(rows)
    delta      = _perf_delta(rows, params)
    memory_ctx = _memory_context(n_recent=10)   # last 10 memory entries (keep prompt small)
    prompt     = _build_prompt(rows, stats, hours, params, delta, memory_ctx)

    try:
        t0   = time.time()
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":   MODEL,
                "system":  _SYSTEM_PROMPT,
                "prompt":  prompt,
                "stream":  False,
                "options": {
                    "temperature":  0.3,
                    "num_predict":  400,   # 3B @ ~6-8 tok/s on cax41 = ~50-65s
                },
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
        logger.warning(f"[ANALYST] Parse failed: {exc}\nRaw: {raw[:500]}")
        return None

    # ── Apply trading params ───────────────────────────────────────────────
    new = dict(params)

    def _f(key, lo, hi, fallback=None):
        """Safely extract a float from the LLM payload with clamping."""
        val = payload.get(key)
        if val is None:
            return fallback
        try:
            return float(max(lo, min(hi, float(val))))
        except (ValueError, TypeError):
            logger.debug(f"[ANALYST] Invalid {key!r} from LLM: {val!r}")
            return fallback

    # signal_threshold is intentionally NOT applied here — it's a no-op fallback
    # that only fires for assets not in asset_thresholds (which is never the case).
    # The LLM consistently misunderstands its direction so we discard it entirely.
    if _f("min_trend_score", 0.0, 0.75) is not None:
        new["min_trend_score"] = _f("min_trend_score", 0.0, 0.75)
    if "skip_assets" in payload and isinstance(payload["skip_assets"], list):
        new["skip_assets"] = [str(s).upper() for s in payload["skip_assets"]]
    if "prefer_assets" in payload and isinstance(payload["prefer_assets"], list):
        new["prefer_assets"] = [str(s).upper() for s in payload["prefer_assets"]]
    try:
        if "max_secs_in" in payload and payload["max_secs_in"] is not None:
            new["max_secs_in"] = int(max(120, min(240, int(payload["max_secs_in"]))))
    except (ValueError, TypeError):
        pass
    if "kelly_override" in payload:
        ko = payload["kelly_override"]
        try:
            new["kelly_override"] = float(max(0.05, min(0.5, float(ko)))) if ko is not None else None
        except (ValueError, TypeError):
            pass
    if "time_of_day_skip" in payload and isinstance(payload["time_of_day_skip"], list):
        try:
            new["time_of_day_skip"] = [int(h) % 24 for h in payload["time_of_day_skip"]]
        except (ValueError, TypeError):
            pass
    if "asset_thresholds" in payload and isinstance(payload["asset_thresholds"], dict):
        cleaned = {}
        for k, v in payload["asset_thresholds"].items():
            try:
                cleaned[str(k).upper()] = float(max(0.00005, min(0.005, float(v))))
            except (ValueError, TypeError):
                pass
        new["asset_thresholds"] = cleaned
    try:
        if "min_price_history_s" in payload and payload["min_price_history_s"] is not None:
            new["min_price_history_s"] = int(max(60, min(300, int(payload["min_price_history_s"]))))
    except (ValueError, TypeError):
        pass

    # ── Self-improvement ───────────────────────────────────────────────────
    ww_mrd    = str(payload.get("ww_mrd_action", ""))
    reasoning = str(payload.get("reasoning", ""))
    strategy  = str(payload.get("analysis_strategy", ""))
    mem_entry = str(payload.get("memory_entry", ""))

    if ww_mrd:
        new["ww_mrd_action"] = ww_mrd
    if strategy:
        new["analysis_strategy"] = strategy  # no cap — let it grow

    new["_runs"]              = new.get("_runs", 0) + 1
    new["_last_run_at"]       = int(time.time())
    new["_last_reasoning"]    = reasoning
    new["trades_at_last_run"] = len(rows)
    new["wins_at_last_run"]   = sum(1 for r in rows if r["win"])

    save_params(new)

    # ── Apply code patch if proposed ──────────────────────────────────────
    code_patch = str(payload.get("code_patch", "")).strip()
    if code_patch:
        patched = _apply_patch(code_patch)
        if patched:
            # Restart the bot process so the new code takes effect.
            # pm2 will automatically relaunch it.
            logger.warning("[ANALYST] Code patch applied — restarting bot to load new code…")
            os.kill(os.getpid(), signal.SIGTERM)

    # ── Append to unlimited memory ─────────────────────────────────────────
    if mem_entry:
        _append_memory({
            "run":         new["_runs"],
            "ts":          new["_last_run_at"],
            "observation": mem_entry,
            "ww_mrd":      ww_mrd,
            "params_set":  {k: new[k] for k in (
                "signal_threshold","min_trend_score","skip_assets",
                "prefer_assets","max_secs_in","kelly_override",
                "time_of_day_skip","asset_thresholds"
            )},
            "perf_delta":  delta,
        })

    # ── Append to full history ─────────────────────────────────────────────
    _append_history({
        "run":       new["_runs"],
        "ts":        new["_last_run_at"],
        "elapsed_s": round(elapsed, 1),
        "trades":    len(rows),
        "win_rate":  round(sum(1 for r in rows if r["win"]) / len(rows), 3),
        "ww_mrd":    ww_mrd,
        "reasoning": reasoning,
        "memory_entry": mem_entry,
        "perf_delta":   delta,
        "params_applied": {k: new[k] for k in (
            "signal_threshold","min_trend_score","skip_assets",
            "prefer_assets","max_secs_in","kelly_override",
            "time_of_day_skip","asset_thresholds","min_price_history_s"
        )},
    })

    logger.warning(
        f"\n{'━'*62}\n"
        f"[ANALYST] Run #{new['_runs']} — {elapsed:.1f}s\n"
        f"  WW_MRD   : {ww_mrd}\n"
        f"  reasoning: {reasoning}\n"
        f"  threshold: {new['signal_threshold']:.4f}  "
        f"min_trend: {new['min_trend_score']:.2f}  "
        f"kelly: {new.get('kelly_override')}\n"
        f"  skip     : {new['skip_assets']}  "
        f"prefer: {new['prefer_assets']}\n"
        f"  tod_skip : {new.get('time_of_day_skip',[])}  "
        f"asset_thresh: {new.get('asset_thresholds',{})}\n"
        f"  memory   : {mem_entry[:120]}\n"
        f"{'━'*62}"
    )
    return new
