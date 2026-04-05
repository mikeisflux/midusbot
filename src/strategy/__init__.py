"""
src/strategy — trading strategy package.

Sub-modules:
  constants        — asset detection map and per-asset threshold dicts
  signal_model     — TradeSignal dataclass
  market_utils     — _detect_updown_market, _updown_window_mins, _market_seconds_into_window
  analyst_cache    — TTL-cached analyst_params.json reader
  prob_stats       — Bayesian / Markov / Normal-vol / binomial statistical modifiers
  updown_momentum  — UpDownMomentumStrategy (oracle-lag cold-start)
  trend_follow     — TrendFollowStrategy (per-asset win-streak tracker)

All public names are re-exported here for backward compatibility — existing
imports like `from src.strategy import TradeSignal` continue to work unchanged.
"""
from __future__ import annotations

# ── Public re-exports ────────────────────────────────────────────────────────
from src.strategy.constants import (
    _UPDOWN_ASSETS,
    _MIN_MOMENTUM_PCT,
    _MIN_WINDOW_RETURN_PCT,
    _GLOBAL_THRESHOLD_CEIL,
    _DEFAULT_ASSET_THRESHOLDS,
    _ASSET_THRESHOLD_CEILS,
    _ASSET_THRESHOLD_FLOORS,
)
from src.strategy.signal_model import TradeSignal
from src.strategy.market_utils import (
    _detect_updown_market,
    _updown_window_mins,
    _market_seconds_into_window,
)
from src.strategy.analyst_cache import _load_analyst_params_cached
from src.strategy.prob_stats import (
    _load_prob_stats,
    _bayesian_win_rate_mult,
    _normal_vol_mult,
    _markov_persistence_mult,
    _binomial_streak_confidence,
)
# signals re-exports — scanner.py and bot.py import these from src.strategy
# because the old strategy.py imported them at module level
from src.signals import (
    _fetch_price,
    _PRICE_HISTORY,
    _PRICE_LOCK,
)
from src.strategy.updown_momentum import UpDownMomentumStrategy
from src.strategy.trend_follow import TrendFollowStrategy

__all__ = [
    "TradeSignal",
    "UpDownMomentumStrategy",
    "TrendFollowStrategy",
    "_detect_updown_market",
    "_updown_window_mins",
    "_market_seconds_into_window",
    "_load_analyst_params_cached",
    "_UPDOWN_ASSETS",
    "_MIN_MOMENTUM_PCT",
    "_MIN_WINDOW_RETURN_PCT",
    "_GLOBAL_THRESHOLD_CEIL",
    "_DEFAULT_ASSET_THRESHOLDS",
    "_ASSET_THRESHOLD_CEILS",
    "_ASSET_THRESHOLD_FLOORS",
    "_load_prob_stats",
    "_bayesian_win_rate_mult",
    "_normal_vol_mult",
    "_markov_persistence_mult",
    "_binomial_streak_confidence",
    # signals forwarded for backward compat
    "_fetch_price",
    "_PRICE_HISTORY",
    "_PRICE_LOCK",
]
