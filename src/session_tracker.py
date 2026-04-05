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

# Registered callbacks fired when a window closes: fn(symbol, actual_direction, had_signal)
_window_close_callbacks: list = []


def register_window_close_callback(fn) -> None:
    """Register a function to call each time a 5-min window closes for any asset."""
    _window_close_callbacks.append(fn)


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
    pct_change = (close_price - open_price) / open_price
    if abs(pct_change) < 1e-8:
        return  # flat window — exclude ties from accuracy calculation
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

    # Fire registered callbacks so TrendTracker stays current every window.
    for fn in _window_close_callbacks:
        try:
            fn(symbol, actual_direction, signal_direction is not None)
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


# ---------------------------------------------------------------------------
# Intra-window timing calibration
# ---------------------------------------------------------------------------

def update_with_timing(
    symbol: str,
    current_price: float,
    secs_into_window: float,
    signal_direction: str | None = None,
) -> None:
    """
    Enhanced update that also records seconds-into-window for timing calibration.
    Appends a 'secs_in' field to the session log so we can learn which entry
    times have the highest accuracy per asset.
    """
    sym = symbol.upper()
    win_start = _window_start_now()

    with _lock:
        if sym in _active:
            entry_start, open_price, entry_dir = _active[sym]
            if win_start > entry_start + 10:
                _close_window_with_timing(sym, entry_start, open_price, current_price, entry_dir)
                _active[sym] = (win_start, current_price, signal_direction)
            else:
                if signal_direction is not None and entry_dir is None:
                    _active[sym] = (entry_start, open_price, signal_direction)
        else:
            _active[sym] = (win_start, current_price, signal_direction)


def _close_window_with_timing(
    symbol: str,
    open_ts: float,
    open_price: float,
    close_price: float,
    signal_direction: str | None,
    secs_in: float | None = None,
) -> None:
    if open_price <= 0:
        return
    pct_change = (close_price - open_price) / open_price
    if abs(pct_change) < 1e-8:
        return  # flat window — exclude ties from accuracy calculation
    actual_direction = "UP" if pct_change > 0 else "DOWN"
    entry = {
        "symbol":           symbol,
        "window_start":     open_ts,
        "open_price":       open_price,
        "close_price":      close_price,
        "pct_change":       round(pct_change, 6),
        "actual_direction": actual_direction,
        "signal_direction": signal_direction,
        "signal_correct":   (signal_direction == actual_direction)
                            if signal_direction is not None else None,
        "secs_in":          secs_in,
        "ts":               time.time(),
    }
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_timing_accuracy(symbol: str, lookback: int = 200) -> dict:
    """
    Returns signal accuracy broken down by seconds-into-window buckets.
    Useful for learning the optimal entry time per asset.

    Returns dict: {bucket_label: {accuracy, count}}
    Buckets: 0-30s, 30-60s, 60-120s, 120-180s, 180-240s
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
                    if e.get("symbol") == sym and e.get("signal_direction") and e.get("secs_in") is not None:
                        entries.append(e)
                except Exception:
                    pass
    except Exception:
        return {}

    recent = entries[-lookback:]
    buckets: dict[str, dict] = {
        "0-30s":   {"correct": 0, "total": 0},
        "30-60s":  {"correct": 0, "total": 0},
        "60-120s": {"correct": 0, "total": 0},
        "120-180s":{"correct": 0, "total": 0},
        "180-240s":{"correct": 0, "total": 0},
    }
    for e in recent:
        s = e["secs_in"]
        if s < 30:     bkt = "0-30s"
        elif s < 60:   bkt = "30-60s"
        elif s < 120:  bkt = "60-120s"
        elif s < 180:  bkt = "120-180s"
        else:          bkt = "180-240s"
        buckets[bkt]["total"] += 1
        if e.get("signal_correct"):
            buckets[bkt]["correct"] += 1

    return {
        k: {
            "accuracy": round(v["correct"] / v["total"], 3) if v["total"] > 0 else None,
            "count": v["total"],
        }
        for k, v in buckets.items()
    }


# ---------------------------------------------------------------------------
# BTC leadership lag calibration
# ---------------------------------------------------------------------------

_BTC_PRICE_LOG: list[tuple[float, float]] = []   # (price, ts) for BTC
_ALT_PRICE_LOG: dict[str, list[tuple[float, float, float]]] = {}  # sym → [(btc_price, alt_price, ts)]
_LAG_LOCK = threading.Lock()


def record_for_lag_calibration(btc_price: float, alt_symbol: str, alt_price: float) -> None:
    """
    Record BTC and alt prices together for lag calibration.
    Call every scan cycle with current prices.
    """
    sym = alt_symbol.upper()
    now = time.time()
    with _LAG_LOCK:
        _BTC_PRICE_LOG.append((btc_price, now))
        # Keep only last 600s
        cutoff = now - 600
        while _BTC_PRICE_LOG and _BTC_PRICE_LOG[0][1] < cutoff:
            _BTC_PRICE_LOG.pop(0)
        log = _ALT_PRICE_LOG.setdefault(sym, [])
        log.append((btc_price, alt_price, now))
        while log and log[0][2] < cutoff:
            log.pop(0)


def get_btc_lead_lag(alt_symbol: str, max_lag_secs: int = 60) -> float | None:
    """
    Estimate how many seconds BTC leads alt_symbol using cross-correlation
    of price return series. Returns lag in seconds (positive = BTC leads).
    None if insufficient data.
    """
    sym = alt_symbol.upper()
    with _LAG_LOCK:
        log = list(_ALT_PRICE_LOG.get(sym, []))

    if len(log) < 30:
        return None

    import numpy as np
    # Compute 15s-binned returns for BTC and alt
    bin_size = 15
    bins: dict[int, dict] = {}
    for btc_p, alt_p, ts in log:
        b = int(ts // bin_size)
        bins.setdefault(b, []).append((btc_p, alt_p))

    sorted_bins = sorted(bins.keys())
    if len(sorted_bins) < 10:
        return None

    btc_returns = []
    alt_returns = []
    for i in range(1, len(sorted_bins)):
        b0, b1 = sorted_bins[i-1], sorted_bins[i]
        prev = bins[b0]
        curr = bins[b1]
        if not prev or not curr:
            continue
        btc_r = (curr[0][0] - prev[0][0]) / prev[0][0] if prev[0][0] > 0 else 0
        alt_r = (curr[0][1] - prev[0][1]) / prev[0][1] if prev[0][1] > 0 else 0
        btc_returns.append(btc_r)
        alt_returns.append(alt_r)

    if len(btc_returns) < 8:
        return None

    btc_arr = np.array(btc_returns)
    alt_arr = np.array(alt_returns)
    # Cross-correlate to find the lag where correlation is highest
    max_shift = min(max_lag_secs // bin_size, len(btc_arr) // 2)
    best_corr, best_lag = 0.0, 0
    for shift in range(0, max_shift + 1):
        if shift == 0:
            corr = float(np.corrcoef(btc_arr, alt_arr)[0, 1])
        else:
            corr = float(np.corrcoef(btc_arr[:-shift], alt_arr[shift:])[0, 1])
        if abs(corr) > abs(best_corr):
            best_corr, best_lag = corr, shift
    return float(best_lag * bin_size)  # convert bins → seconds
