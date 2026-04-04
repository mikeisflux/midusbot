"""
Fair value and edge computation for UpDown crypto strategies.

Provides:
  - compute_updown_fair_value  — momentum-based fair prob for UpDownMomentumStrategy
  - compute_trendfollow_fair_value — streak-based fair prob for TrendFollowStrategy
"""
from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# UpDownMomentumStrategy fair value
# ---------------------------------------------------------------------------

def compute_updown_fair_value(
    window_return: float,
    trend_score: float | None,
    trend_dir_ok: bool,
) -> float:
    """
    Estimate the fair probability for the direction implied by window_return.

    Args:
        window_return:  Price change from window-open to now (signed, e.g. +0.002).
        trend_score:    Consecutive-window trend score (-1..+1) or None if unknown.
        trend_dir_ok:   True when trend_score direction agrees with window_return.

    Returns:
        Fair probability (0.53–0.73) for the signal direction (YES for UP, NO for DOWN).
    """
    strength = min(abs(window_return) / 0.01, 1.0)
    # Base 53% → 68%; add up to +5% when trend agrees (consecutive windows)
    trend_boost = 0.05 * abs(trend_score) if (trend_score is not None and trend_dir_ok) else 0.0
    fair_prob = 0.53 + strength * 0.15 + trend_boost
    return float(np.clip(fair_prob, 0.53, 0.73))


# ---------------------------------------------------------------------------
# TrendFollowStrategy fair value
# ---------------------------------------------------------------------------

# Baseline and per-streak increment (mirrors TrendFollowStrategy class constants)
_TREND_FAIR_VALUE_BASE = 0.63
_TREND_STREAK_BONUS    = 0.02   # +2% per streak step
_TREND_FAIR_VALUE_MAX  = 0.72


def compute_trendfollow_fair_value(streak: int, cw_boost: int = 0) -> float:
    """
    Estimate the fair probability for TrendFollowStrategy given the current
    win-streak length and optional consecutive-window boost.

    Args:
        streak:    Number of consecutive wins in the same direction (≥ 1).
        cw_boost:  Extra streak increment when consecutive-window trend agrees
                   (0 or 1).

    Returns:
        Fair probability capped at _TREND_FAIR_VALUE_MAX.
    """
    return min(
        _TREND_FAIR_VALUE_BASE + (streak + cw_boost) * _TREND_STREAK_BONUS,
        _TREND_FAIR_VALUE_MAX,
    )
