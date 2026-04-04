"""
Session outcome tracker for 5-min UpDown windows.

Records the actual price direction for every 5-minute Polymarket window,
independent of whether the bot traded it. This gives ground-truth signal
accuracy data that is NOT contaminated by:
  - Entry price / slippage
  - Position sizing
  - Multiple bets per window

Window outcomes are logged to data/session_log.jsonl and consumed by the
adaptive learner's per-asset threshold calibration.

How it works:
  - strategy.py calls update() every time an asset is scanned
  - Tracker detects 5-min window boundaries from the system clock
    (Polymarket windows are aligned to :00/:05/:10/... minutes, always)
  - When a new window starts, the previous window's outcome is logged
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOG_FILE   = Path("data/session_log.jsonl")
_WINDOW_SEC = 300  # 5 minutes

# symbol → (window_start_ts, open_price, signal_direction_this_window)
_active: dict[str, tuple[float, float, str | None]] = {}
_lock   = threading.Lock()


def _window_start_now() -> float:
    """UTC timestamp of the start of the current 5-min window."""
    now = time.time()
    return now - (now % _WINDOW_SEC)


def update(
    symbol: str,
    current_price: float,
    signal_direction: str | None = None,
) -> None:
    """
    Call every time an asset is scanned in strategy.analyse().

    symbol:           asset symbol ("BTC", "SOL", …)
    current_price:    live Binance price at scan time
    signal_direction: "UP" or "DOWN" if a signal fired this scan, else None
    """
    sym         = symbol.upper()
    win_start   = _window_start_now()

    with _lock:
        if sym in _active:
            entry_start, open_price, entry_dir = _active[sym]
            # New window detected (clock crossed a 5-min boundary)
            if win_start > entry_start + 10:   # 10 s buffer for scan timing jitter
                _close_window(sym, entry_start, open_price, current_price, entry_dir)
                _active[sym] = (win_start, current_price, signal_direction)
            else:
                # Same window — record signal direction if one just fired
                if signal_direction is not None and entry_dir is None:
                    _active[sym] = (entry_start, open_price, signal_direction)
        else:
            _active[sym] = (win_start, current_price, signal_direction)


def _close_window(
    symbol: str,
    open_ts: float,
    open_price: float,
    close_price: float,
    signal_direction: str | None,
) -> None:
    if open_price <= 0:
        return
    pct_change       = (close_price - open_price) / open_price
    actual_direction = "UP" if pct_change > 0 else "DOWN"
    entry = {
        "symbol":           symbol,
        "window_start":     open_ts,
        "open_price":       open_price,
        "close_price":      close_price,
        "pct_change":       round(pct_change, 6),
        "actual_direction": actual_direction,
        "signal_direction": signal_direction,
        # True/False if we had a signal; None if no signal this window
        "signal_correct":   (signal_direction == actual_direction)
                            if signal_direction is not None else None,
        "ts": time.time(),
    }
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_asset_stats(symbol: str, lookback: int = 60) -> dict:
    """
    Read session_log.jsonl and return per-signal accuracy stats for one asset.

    Returns dict with:
      total_windows:    total logged windows
      signal_windows:   windows where the bot generated a signal
      signal_correct:   signals that got direction right
      signal_accuracy:  correct / signal_windows  (0.0 if no signals)
      avg_move_pct:     average absolute % move per window
    """
    sym = symbol.upper()
    if not _LOG_FILE.exists():
        return {}
    entries = []
    try:
        with _LOG_FILE.open() as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    if e.get("symbol") == sym:
                        entries.append(e)
                except Exception:
                    pass
    except Exception:
        return {}

    recent   = entries[-lookback:]
    if not recent:
        return {}
    signaled = [e for e in recent if e.get("signal_direction") is not None]
    correct  = [e for e in signaled if e.get("signal_correct")]
    return {
        "total_windows":   len(recent),
        "signal_windows":  len(signaled),
        "signal_correct":  len(correct),
        "signal_accuracy": round(len(correct) / len(signaled), 3) if signaled else 0.0,
        "avg_move_pct":    round(
            sum(abs(e["pct_change"]) for e in recent) / len(recent), 5
        ),
    }


def get_all_stats(lookback: int = 60) -> dict[str, dict]:
    """Return accuracy stats for every tracked asset."""
    if not _LOG_FILE.exists():
        return {}
    symbols: set[str] = set()
    try:
        with _LOG_FILE.open() as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    if s := e.get("symbol"):
                        symbols.add(s)
                except Exception:
                    pass
    except Exception:
        return {}
    return {s: get_asset_stats(s, lookback) for s in sorted(symbols)}
