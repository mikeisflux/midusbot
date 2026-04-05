"""
Advanced probability / statistics signal modifiers.

Four modifiers, all reading data/session_log.jsonl (written by session_tracker):

  1. _bayesian_win_rate_mult  — Beta(2,2) posterior win rate per asset
  2. _normal_vol_mult         — Normal distribution z-score vs historical volatility
  3. _markov_persistence_mult — P(same dir next window | same dir last window)
  4. _binomial_streak_confidence — P(k consecutive wins | p=0.5) significance gate

All functions fall back to neutral (1.0 or unchanged confidence) when
session_log.jsonl doesn't exist or has fewer than 10 windows — so a fresh
bot behaves identically to before until data accumulates.
"""
from __future__ import annotations

import json as _json
import threading as _threading
import time
from pathlib import Path as _Path

import numpy as np
from loguru import logger


_PROB_CACHE: dict[str, tuple[dict, float]] = {}
_PROB_LOCK   = _threading.Lock()
_PROB_CACHE_TTL = 60.0   # re-read log at most once per minute per asset


def _load_prob_stats(symbol: str, lookback: int = 100) -> dict:
    """
    Read session_log.jsonl once per minute per asset.
    Returns a pre-computed stats dict consumed by all four modifiers.
    """
    sym = symbol.upper()
    now = time.time()
    with _PROB_LOCK:
        cached = _PROB_CACHE.get(sym)
        if cached and now - cached[1] < _PROB_CACHE_TTL:
            return cached[0]

    entries: list[dict] = []
    log_path = _Path("data/session_log.jsonl")
    if log_path.exists():
        try:
            with log_path.open() as fh:
                for line in fh:
                    try:
                        e = _json.loads(line)
                        if e.get("symbol") == sym:
                            entries.append(e)
                    except Exception:
                        pass
        except Exception:
            pass

    recent = entries[-lookback:]

    # Directional counts from ALL windows (not just signaled ones).
    # session_tracker records actual_direction for every 5-min window it
    # observes, regardless of whether the bot traded.  This gives hundreds
    # of data points per asset vs. the handful of actual trades.
    directions = [e["actual_direction"] for e in recent if e.get("actual_direction")]
    up_count   = sum(1 for d in directions if d == "UP")
    down_count = len(directions) - up_count

    # Normal vol: distribution of |pct_change| per 5-min window (all windows)
    moves    = [abs(e["pct_change"]) for e in recent if e.get("pct_change") is not None]
    vol_mean = float(np.mean(moves)) if len(moves) >= 15 else None
    vol_std  = float(np.std(moves))  if len(moves) >= 15 else None

    # Also keep signal accuracy for debug/logging
    signaled = [e for e in recent if e.get("signal_direction") is not None]
    sig_wins  = sum(1 for e in signaled if e.get("signal_correct"))

    stats: dict = {
        "up_count":     up_count,
        "down_count":   down_count,
        "n_windows":    len(directions),
        "vol_mean":     vol_mean,
        "vol_std":      vol_std,
        "n_moves":      len(moves),
        "directions":   directions,
        # legacy — still used for debug logging
        "bayes_wins":   sig_wins,
        "bayes_losses": len(signaled) - sig_wins,
        "n_signaled":   len(signaled),
    }
    with _PROB_LOCK:
        _PROB_CACHE[sym] = (stats, now)
    return stats


def _bayesian_win_rate_mult(symbol: str, signal_dir: str = "UP") -> float:
    """
    1. Bayesian directional base-rate multiplier.

    Uses ALL logged window outcomes (actual_direction) — not just windows
    where the bot traded.  session_tracker records every 5-min window close
    for every scanned asset, giving hundreds of data points per asset.

    Measures: P(window goes signal_dir) from the historical base rate.

      count_signal = windows that went signal_dir direction
      count_opp    = windows that went the opposite direction
      posterior    = Beta(2+count_signal, 2+count_opp) mean
                   = (2 + count_signal) / (4 + total_windows)
      multiplier   = 1.0 + (posterior − 0.5) × 1.5
      clamped to [0.5, 1.5]

    Example: BTC went UP 55 of last 100 windows:
      posterior for UP  = 57/104 = 0.548 → ×1.07 (slight boost)
      posterior for DOWN = 47/104 = 0.452 → ×0.93 (slight penalty)

    Neutral (×1.0) when fewer than 10 windows logged.
    """
    s           = _load_prob_stats(symbol)
    n_windows   = s["n_windows"]
    count_sig   = s["up_count"]   if signal_dir == "UP" else s["down_count"]
    count_opp   = s["down_count"] if signal_dir == "UP" else s["up_count"]

    if n_windows < 10:
        logger.debug(
            f"[LOGIC:BAYES:{symbol}] n={n_windows} windows (need 10) → mult=1.00× (neutral)"
        )
        return 1.0

    posterior_mean = (2.0 + count_sig) / (4.0 + count_sig + count_opp)
    mult = 1.0 + (posterior_mean - 0.5) * 1.5
    mult = float(np.clip(mult, 0.5, 1.5))
    logger.debug(
        f"[LOGIC:BAYES:{symbol}] dir={signal_dir}  "
        f"{count_sig}/{n_windows} windows went {signal_dir}  "
        f"posterior={posterior_mean:.3f}  → mult={mult:.3f}×  "
        f"({'favourable' if posterior_mean > 0.5 else 'unfavourable'})"
    )
    return mult


def _normal_vol_mult(symbol: str, window_return: float) -> float:
    """
    2. Normal distribution volatility z-score multiplier.

    Models |pct_change| per 5-min window as Normal(μ, σ) from session_log.

      z = (|signal| − μ) / σ
      multiplier = 1.0 + (z − 1.5) × 0.2
      clamped to [0.5, 1.8]

    z = 1.5 → breakeven (×1.0).
    z = 2.0 → signal is 0.5σ above breakeven → ×1.1 boost.
    z = 0.5 → signal is within typical noise band → ×0.8 penalty.

    Falls back to 1.0 when fewer than 15 windows are logged.
    """
    s = _load_prob_stats(symbol)
    vol_mean = s["vol_mean"]
    vol_std  = s["vol_std"]
    n_moves  = s["n_moves"]
    if vol_mean is None or vol_std is None or vol_std < 1e-8:
        logger.debug(
            f"[LOGIC:VOL:{symbol}] insufficient data (n={n_moves}, need 15) → mult=1.00× (neutral)"
        )
        return 1.0
    z    = (abs(window_return) - vol_mean) / vol_std
    mult = float(np.clip(1.0 + (z - 1.5) * 0.2, 0.5, 1.8))
    logger.debug(
        f"[LOGIC:VOL:{symbol}] signal={abs(window_return):.4%}  "
        f"hist_mean={vol_mean:.4%} hist_std={vol_std:.4%} n={n_moves}  "
        f"z={z:.2f}  → mult={mult:.3f}×  "
        f"({'above noise' if z > 1.5 else 'within typical noise band'})"
    )
    return mult


def _markov_persistence_mult(symbol: str, signal_dir: str) -> float:
    """
    3. Markov chain persistence multiplier.

    Estimates P(window_t+1 = signal_dir | window_t = signal_dir) from the
    last 50 window outcomes in session_log using Laplace smoothing (+1/+2).

      P > 0.5 → trend-following asset → boost (up to ×1.4)
      P = 0.5 → random walk           → neutral (×1.0)
      P < 0.5 → mean-reverting asset  → penalise (down to ×0.6)

    Falls back to 1.0 if fewer than 5 same-direction transitions observed.
    """
    s          = _load_prob_stats(symbol)
    directions = s["directions"][-50:]
    n_dirs     = len(directions)
    if n_dirs < 10:
        logger.debug(
            f"[LOGIC:MARKOV:{symbol}] only {n_dirs} direction observations (need 10) → mult=1.00× (neutral)"
        )
        return 1.0

    same = total = 0
    for i in range(1, len(directions)):
        if directions[i - 1] == signal_dir:
            total += 1
            if directions[i] == signal_dir:
                same += 1

    if total < 5:
        logger.debug(
            f"[LOGIC:MARKOV:{symbol}] only {total} prior {signal_dir} windows (need 5) → mult=1.00× (neutral)"
        )
        return 1.0

    persistence = (same + 1) / (total + 2)   # Laplace smoothing
    mult = float(np.clip(0.6 + persistence * 0.8, 0.6, 1.4))
    logger.debug(
        f"[LOGIC:MARKOV:{symbol}] dir={signal_dir}  same={same}/{total} transitions  "
        f"persistence={persistence:.3f}  → mult={mult:.3f}×  "
        f"({'trend-following' if persistence > 0.5 else 'mean-reverting'})"
    )
    return mult


def _binomial_streak_confidence(streak: int) -> str:
    """
    4. Binomial significance gate for win-streak confidence.

    P(k consecutive wins | p=0.5) = 0.5^k (assuming independence).

      k ≥ 5  → p < 0.031 → HIGH   (statistically significant at 5% level)
      k = 3–4 → p < 0.125 → MEDIUM (borderline)
      k ≤ 2  → p ≥ 0.25  → LOW    (easily due to chance)
    """
    p_value = 0.5 ** streak
    if p_value < 0.05:    # streak ≥ 5
        return "HIGH"
    elif p_value < 0.25:  # streak 3–4
        return "MEDIUM"
    else:                  # streak 1–2
        return "LOW"
