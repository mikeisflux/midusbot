"""
Trading strategies for Polymarket — UpDown crypto markets only.

Two strategies:
  1. TrendFollowStrategy  — follow per-asset win streaks + momentum confirm
  2. UpDownMomentumStrategy — Binance multi-timeframe momentum (cold start)

Price-feed infrastructure lives in src.signals.
Fair-value computation lives in src.fair_value.
"""
from __future__ import annotations

import json as _json
import re as _re
import threading as _threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path as _Path

import numpy as np
from loguru import logger

from src.client import Market, OrderBook
from src import session_tracker as _st
from src.signals import (
    _PRICE_HISTORY,
    _PRICE_LOCK,
    _fetch_price,
    _window_return,
    _consecutive_window_trend,
    _btc_leadership_signal,
    _exchange_pressure,
    _price_acceleration,
    _multitf_consensus,
    _get_trade_rate,
    _get_avg_trade_rate,
    _market_regime,
    _get_funding_rate,
    _get_oi_delta,
    _cross_exchange_divergence,
)
from src.fair_value import compute_updown_fair_value, compute_trendfollow_fair_value
import config


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class TradeSignal:
    market_id: str
    question: str
    side: str            # "YES" | "NO"
    token_id: str
    market_price: float
    fair_value: float
    edge: float
    signal: float
    confidence: str      # "LOW" | "MEDIUM" | "HIGH"
    momentum_signal: float  = 0.0
    imbalance_signal: float = 0.0
    is_latency_arb: bool = False
    lag_pct: float = 0.0
    hours_to_close: float | None = None
    best_ask: float | None = None
    best_bid: float | None = None
    no_best_ask: float | None = None   # NO token ask (fetched separately for NO trades)
    is_news_arb: bool = False
    secs_into_window: float = 0.0
    rel_strength: float = 0.0   # |window_return| / threshold — used for best-signal ranking
    win_mins: int = 5            # window duration in minutes (5 or 15)

    def __str__(self) -> str:
        return (
            f"[{self.confidence}] {self.side} {self.question[:60]} | "
            f"mkt={self.market_price:.3f}  fv={self.fair_value:.3f}  "
            f"edge={self.edge:+.3f}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# UpDown market detection helpers
# ═══════════════════════════════════════════════════════════════════════════

# Maps keywords in the market question to the Binance symbol to fetch
_UPDOWN_ASSETS = {
    "xrp":          "XRP",
    "ripple":       "XRP",
    "btc":          "BTC",
    "bitcoin":      "BTC",
    "eth":          "ETH",
    "ethereum":     "ETH",
    "sol":          "SOL",
    "solana":       "SOL",
    "doge":         "DOGE",
    "dogecoin":     "DOGE",
    "bnb":          "BNB",
    "hype":         "HYPE",
    "hyperliquid":  "HYPE",
    "avax":         "AVAX",
    "avalanche":    "AVAX",
    "link":         "LINK",
    "chainlink":    "LINK",
    "ada":          "ADA",
    "cardano":      "ADA",
    "ltc":          "LTC",
    "litecoin":     "LTC",
    "dot":          "DOT",
    "polkadot":     "DOT",
    "matic":        "MATIC",
    "pol":          "MATIC",
    "polygon":      "MATIC",
    "sui":          "SUI",
    "pepe":         "PEPE",
    "wif":          "WIF",
    "dogwifhat":    "WIF",
    "trx":          "TRX",
    "tron":         "TRX",
}

# Minimum absolute 60s momentum to act on (0.05% move in 60s)
_MIN_MOMENTUM_PCT = 0.0005

# Global fallback floor — only used when no per-asset threshold exists
# and analyst hasn't set a global signal_threshold.
_MIN_WINDOW_RETURN_PCT = 0.0008

# Per-asset default thresholds informed by dry-run history (23 trades).
# SOL: only winning asset (25% wr) — keep accessible at 0.035%.
# DOGE: 0/5 real trades, worst P&L overall — needs 0.10% minimum.
# XRP: 0/2 real trades, largest single-trade loss — needs 0.08%.
# BTC/ETH/BNB: weak signals, small sample — 0.035-0.04%.
# HYPE: insufficient data — conservative 0.08%.
# Analyst and learner can raise or lower these at any time.
_DEFAULT_ASSET_THRESHOLDS: dict[str, float] = {
    "BTC":  0.00035,  # 0.035% — stable large-cap, tiny signals
    "ETH":  0.0004,   # 0.04%  — 0/2 trades, raise floor slightly
    "BNB":  0.0004,   # 0.04%  — 0/1 trade, raise floor slightly
    "XRP":  0.0008,   # 0.08%  — 0/2, lost hard on strongest signal
    "SOL":  0.00035,  # 0.035% — ONLY winner; keep accessible
    "DOGE": 0.0010,   # 0.10%  — 0/5, systematic losses; severe tightening
    "HYPE": 0.0008,   # 0.08%  — insufficient data; conservative
}

# Hard ceiling on the GLOBAL signal_threshold — analyst can never raise it
# above this.  0.0015 = 0.15% (3× BTC default).  Above this the bot would
# see essentially zero signals regardless of market conditions.
_GLOBAL_THRESHOLD_CEIL: float = 0.0015

# Hard ceiling on per-asset thresholds — analyst can raise thresholds to
# filter noise but cannot set them so high that the asset never trades.
_ASSET_THRESHOLD_CEILS: dict[str, float] = {
    "BTC":  0.00070,  # never above 0.07% (2× default 0.035%)
    "ETH":  0.00080,  # never above 0.08% (2× default 0.040%)
    "BNB":  0.00080,  # never above 0.08% (2× default 0.040%)
    "XRP":  0.00160,  # never above 0.16% (2× default 0.080%)
    "SOL":  0.00070,  # never above 0.07% (2× default 0.035%)
    "DOGE": 0.00200,  # never above 0.20% (2× default 0.100%)
    "HYPE": 0.00160,  # never above 0.16% (2× default 0.080%)
    "WIF":  0.00240,  # never above 0.24% (2× default 0.120%)
    "TRUMP":0.00300,  # never above 0.30% (2× default 0.150%)
}

# Hard minimums — analyst/LLM can NEVER lower thresholds below these.
# These exist because low thresholds cause the bot to trade on noise ticks.
_ASSET_THRESHOLD_FLOORS: dict[str, float] = {
    "BTC":  0.00025,  # never below 0.025%
    "ETH":  0.00025,
    "BNB":  0.00035,  # never below 0.035%
    "XRP":  0.00060,  # never below 0.06%
    "SOL":  0.00025,
    "DOGE": 0.00080,  # never below 0.08%
    "HYPE": 0.00060,
    "WIF":  0.00080,
    "TRUMP":0.00100,
}


# ═══════════════════════════════════════════════════════════════════════════
# Advanced probability / statistics signal modifiers
# ═══════════════════════════════════════════════════════════════════════════
#
# Four modifiers, all reading data/session_log.jsonl (written by session_tracker):
#
#  1. _bayesian_win_rate_mult  — Beta(2,2) posterior win rate per asset
#  2. _normal_vol_mult         — Normal distribution z-score vs historical volatility
#  3. _markov_persistence_mult — P(same dir next window | same dir last window)
#  4. _binomial_streak_confidence — P(k consecutive wins | p=0.5) significance gate
#
# All functions fall back to neutral (1.0 or unchanged confidence) when
# session_log.jsonl doesn't exist or has fewer than 10 windows — so a fresh
# bot behaves identically to before until data accumulates.
# ═══════════════════════════════════════════════════════════════════════════

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

    # Bayesian: signal win/loss counts
    signaled = [e for e in recent if e.get("signal_direction") is not None]
    wins     = sum(1 for e in signaled if e.get("signal_correct"))
    losses   = len(signaled) - wins

    # Normal vol: distribution of |pct_change| per 5-min window
    moves    = [abs(e["pct_change"]) for e in recent if e.get("pct_change") is not None]
    vol_mean = float(np.mean(moves)) if len(moves) >= 15 else None
    vol_std  = float(np.std(moves))  if len(moves) >= 15 else None

    # Markov: sequence of actual_direction outcomes
    directions = [e["actual_direction"] for e in recent if e.get("actual_direction")]

    stats: dict = {
        "bayes_wins":   wins,
        "bayes_losses": losses,
        "n_signaled":   len(signaled),
        "vol_mean":     vol_mean,
        "vol_std":      vol_std,
        "n_moves":      len(moves),
        "directions":   directions,
    }
    with _PROB_LOCK:
        _PROB_CACHE[sym] = (stats, now)
    return stats


def _bayesian_win_rate_mult(symbol: str) -> float:
    """
    1. Bayesian win-rate multiplier.

    Uses a Beta(2, 2) prior (weakly centred at 0.5) updated with
    observed signal_correct counts from session_log.

      posterior_mean = (2 + wins) / (4 + wins + losses)
      multiplier     = 1.0 + (posterior_mean − 0.5) × 1.5
      clamped to [0.5, 1.5]

    At posterior 0.5 (no data / 50-50) → ×1.0 (neutral).
    At posterior 0.7 (good accuracy)   → ×1.3 (boost).
    At posterior 0.3 (bad accuracy)    → ×0.7 (penalise).
    """
    s      = _load_prob_stats(symbol)
    wins   = s["bayes_wins"]
    losses = s["bayes_losses"]
    n      = wins + losses
    posterior_mean = (2.0 + wins) / (4.0 + wins + losses)
    mult = 1.0 + (posterior_mean - 0.5) * 1.5
    mult = float(np.clip(mult, 0.5, 1.5))
    logger.debug(
        f"[LOGIC:BAYES:{symbol}] n={n} signals  wins={wins} losses={losses}  "
        f"posterior={posterior_mean:.3f}  → mult={mult:.3f}×  "
        f"({'neutral — no data' if n == 0 else 'boosting accuracy' if posterior_mean > 0.5 else 'penalising bad accuracy'})"
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
      k = 3–4 → p < 0.125 → MEDIUM (borderline; 3-streak is 12.5% luck)
      k ≤ 2  → p ≥ 0.25  → LOW    (easily due to chance)

    Replaces the former "streak ≥ 3 = HIGH" heuristic, which rated a
    12.5%-chance run as a strong signal.
    """
    p_value = 0.5 ** streak
    if p_value < 0.05:    # streak ≥ 5
        return "HIGH"
    elif p_value < 0.25:  # streak 3–4
        return "MEDIUM"
    else:                  # streak 1–2
        return "LOW"


def _detect_updown_market(question: str) -> str | None:
    """
    Returns the asset symbol if the question is an Up/Down short-interval
    crypto market.  Handles all Polymarket question phrasings:
      • "XRP Up or Down - March 3, 12:00PM-12:05PM ET"
      • "Will BTC be higher or lower in 15 minutes?"
      • "Bitcoin higher or lower in the next 5 min?"
      • "BTC up/down 15m"
    Returns None otherwise.
    """
    q = question.lower()
    # Must contain a directional phrase
    direction_phrases = (
        "up or down", "up/down", "higher or lower",
        "higher in", "lower in", "go up", "go down",
        "be up", "be down",
    )
    # Must also reference a short time interval to avoid false positives
    # on long-term questions like "Will BTC be higher by end of year?"
    time_phrases = (
        "5 min", "5min", "15 min", "15min", "5-min", "15-min",
        "5 minute", "15 minute", "next 5", "next 15",
        "in 5", "in 15", ":00pm", ":05pm", ":10pm", ":15pm",
        ":20pm", ":25pm", ":30pm", ":35pm", ":40pm", ":45pm",
        ":50pm", ":55pm", ":00am", ":05am", ":10am", ":15am",
    )
    has_direction = any(p in q for p in direction_phrases)
    has_time      = any(p in q for p in time_phrases)
    # "up or down" alone is strong enough (e.g. "XRP Up or Down - 12:00PM-12:05PM")
    if not has_direction:
        return None
    # For "higher or lower" we require a time phrase to avoid false positives
    if "higher or lower" in q and not has_time:
        return None
    for kw, sym in _UPDOWN_ASSETS.items():
        if kw in q:
            return sym
    return None


def _updown_window_mins(question: str) -> int | None:
    """
    Extract the time-window duration in minutes from an UpDown market question.
    "BTC Up or Down - 3:00AM-3:05AM ET"  → 5
    "BTC Up or Down - 3AM-4AM ET"         → 60
    "BTC 5 Minute Up or Down"             → 5   (new perpetual format)
    "BTC 15 Minute Up or Down"            → 15
    "BTC 1 Hour Up or Down"               → 60
    "BTC 4 Hour Up or Down"               → 240
    Returns None if the format isn't recognised.
    """
    import re as _re2
    q = question.lower()
    # New perpetual format: "N Minute" or "N Hour"
    m = _re2.search(r'(\d+)\s*(minute|min)\b', q)
    if m:
        return int(m.group(1))
    m = _re2.search(r'(\d+)\s*(hour|hr)\b', q)
    if m:
        return int(m.group(1)) * 60
    # Old per-slot format: "H:MMam-H:MMam"
    m = _re2.search(r'(\d{1,2}):(\d{2})\s*[ap]m\s*[-–]\s*(\d{1,2}):(\d{2})\s*[ap]m', q)
    if m:
        h1, mn1, h2, mn2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        delta = (h2 * 60 + mn2) - (h1 * 60 + mn1)
        if delta <= 0:
            delta += 12 * 60  # AM/PM rollover
        return delta
    # Old hourly format: "HAM-H+1AM"
    m = _re2.search(r'(\d{1,2})\s*[ap]m\s*[-–]\s*(\d{1,2})\s*[ap]m', q)
    if m:
        h1, h2 = int(m.group(1)), int(m.group(2))
        delta = (h2 - h1) * 60
        if delta <= 0:
            delta += 12 * 60
        return delta
    return None


def _market_seconds_into_window(market: Market) -> float | None:
    """
    Returns how many seconds have elapsed since this market window opened.
    None if we can't determine timing.
    """
    end_date = getattr(market, "end_date", None) or getattr(market, "end_utc", None)
    if not end_date:
        return None
    try:
        from datetime import timezone as _tz
        end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
        window_mins = _updown_window_mins(market.question) or 5
        start_ts = end_ts - window_mins * 60
        return time.time() - start_ts
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 1: UpDownMomentumStrategy (oracle-lag / cold-start)
# ═══════════════════════════════════════════════════════════════════════════

class UpDownMomentumStrategy:
    """
    Trades Polymarket "XRP Up or Down - HH:MM-HH:MM" style markets.

    Logic:
      1. Detect the asset from the question.
      2. Measure the 60-second live price momentum from Binance.
      3. If momentum is strongly directional AND the market price for that
         direction is below our fair-value estimate, enter.

    Fair value model:
      - A market priced at 50¢ implies a coin flip.
      - If the asset is up +0.2% in the last 60s, we estimate the chance
        of it being "Up" at resolution is ~65%+ (momentum tends to persist
        over 5-min windows in liquid crypto markets).
      - We enter if market_price < fair_value - MIN_EDGE.
    """

    def __init__(self, min_momentum_pct: float = _MIN_MOMENTUM_PCT) -> None:
        self.min_momentum_pct = min_momentum_pct

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        symbol = _detect_updown_market(market.question)
        if symbol is None:
            return None

        # ── Guard 1: Strict timing — must know window start ───────────────────
        secs_in = _market_seconds_into_window(market)
        # Load analyst params (LLM-tuned)
        try:
            from src.analyst import load_params as _load_analyst_params
            _ap = _load_analyst_params()
        except Exception:
            _ap = {}

        _max_secs_in = int(_ap.get("max_secs_in", 240))
        if secs_in is None or secs_in < 5:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] TIMING — secs_in={secs_in} < 5 or unknown → SKIP")
            return None
        window_mins = _updown_window_mins(market.question) or 5
        effective_max_secs = _max_secs_in if window_mins == 5 else int(window_mins * 60 * 0.80)
        if secs_in > effective_max_secs:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] TIMING — {secs_in:.0f}s into {window_mins}min window "
                f"(max={effective_max_secs}s) → SKIP (too late, oracle lag gone)"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] TIMING — {secs_in:.0f}s into {window_mins}min window "
            f"(max={effective_max_secs}s) → PASS"
        )

        # Time-of-day skip
        _tod_skip = _ap.get("time_of_day_skip", [])
        if _tod_skip and time.gmtime().tm_hour in _tod_skip:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] TOD-SKIP — UTC hour {time.gmtime().tm_hour} "
                f"in analyst block-list {_tod_skip} → SKIP"
            )
            return None

        # Price history warmup
        _min_hist = int(_ap.get("min_price_history_s", 30))
        live_price = _fetch_price(symbol)
        if live_price is None:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] PRICE-FEED — no live price from Binance WS → SKIP")
            return None
        _st.update(symbol, live_price)
        hist = _PRICE_HISTORY.get(symbol.upper(), [])
        if len(hist) < 2:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] WARMUP — price history empty (0 ticks) → SKIP")
            return None
        hist_span = hist[-1][1] - hist[0][1]
        if hist_span < _min_hist:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] WARMUP — only {hist_span:.0f}s of price history "
                f"(need {_min_hist}s) → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] WARMUP — {hist_span:.0f}s price history  "
            f"live_price={live_price:.4f} → PASS"
        )

        # ── Guard 2: Entry price must still be near 0.50 ─────────────────────
        mid = order_book.mid if order_book else market.yes_price
        if mid > 0.54 or mid < 0.46:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] ENTRY-GUARD — mid={mid:.3f} outside 0.46-0.54 "
                f"(MMs already repriced) → SKIP"
            )
            return None

        # ── Guard 2b: Order book liquidity check ──────────────────────────────
        # bid=0.01 / ask=0.99 means there's no real market — nobody is quoting.
        # mid=0.50 in this case is a mathematical artefact (0.01+0.99)/2, not a
        # real price. Skip before doing any signal computation.
        if order_book and order_book.best_bid > 0 and order_book.best_ask > 0:
            _book_spread = order_book.best_ask - order_book.best_bid
            if _book_spread > 0.85:
                logger.debug(
                    f"[LOGIC:UPDOWN:{symbol}] BOOK-EMPTY — bid={order_book.best_bid:.3f} "
                    f"ask={order_book.best_ask:.3f} spread={_book_spread:.3f} "
                    f"(no real market, ask-guard would block anyway) → SKIP"
                )
                return None
        logger.debug(f"[LOGIC:UPDOWN:{symbol}] ENTRY-GUARD — mid={mid:.3f} within 0.46-0.54 → PASS")

        # Skip assets on analyst block-list
        if symbol.upper() in [s.upper() for s in _ap.get("skip_assets", [])]:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] SKIP-ASSETS — analyst blocked this asset → SKIP")
            return None

        # Window return + threshold
        with _PRICE_LOCK:
            _hist_now = list(_PRICE_HISTORY.get(symbol.upper(), []))
        if len(_hist_now) >= 2:
            _available_span = _hist_now[-1][1] - _hist_now[0][1]
            _effective_secs = min(int(secs_in), max(15, int(_available_span) - 2))
        else:
            _available_span = 0
            _effective_secs = int(secs_in)

        window_return = _window_return(symbol, _effective_secs)
        # Threshold priority (highest → lowest):
        #   1. analyst/learner per-asset override  (asset_thresholds["BTC"])
        #   2. per-asset default                   (_DEFAULT_ASSET_THRESHOLDS["BTC"])
        #   3. analyst global signal_threshold
        _asset_thresholds = _ap.get("asset_thresholds", {})
        _sym = symbol.upper()
        if _sym in _asset_thresholds:
            _hard_floor = _ASSET_THRESHOLD_FLOORS.get(_sym, 0.00025)
            _hard_ceil  = _ASSET_THRESHOLD_CEILS.get(_sym, 0.00300)
            _sig_thresh = float(max(_hard_floor, min(_hard_ceil, _asset_thresholds[_sym])))
            _thresh_src = f"analyst ({_asset_thresholds[_sym]:.5%}, clamped floor={_hard_floor:.5%} ceil={_hard_ceil:.5%})"
        else:
            _global = float(max(
                _MIN_WINDOW_RETURN_PCT,
                min(_GLOBAL_THRESHOLD_CEIL, _ap.get("signal_threshold", _MIN_WINDOW_RETURN_PCT)),
            ))
            _sig_thresh = float(_DEFAULT_ASSET_THRESHOLDS.get(_sym, _global))
            _thresh_src = f"default (asset default or global fallback)"

        if window_return is None:
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] WIN-RETURN — unavailable "
                f"(lookback={_effective_secs}s hist={int(_available_span)}s) → SKIP"
            )
            return None
        _ratio = abs(window_return) / _sig_thresh if _sig_thresh > 0 else 0.0
        if abs(window_return) < _sig_thresh:
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] THRESHOLD — win_ret={window_return:+.4%} "
                f"thresh={_sig_thresh:.4%} ({_thresh_src}) ratio={_ratio:.2f}× → SKIP (below threshold)"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] THRESHOLD — win_ret={window_return:+.4%} "
            f"thresh={_sig_thresh:.4%} ({_thresh_src}) ratio={_ratio:.2f}× → PASS"
        )

        # ── CONVICTION FILTER ─────────────────────────────────────────────────
        _rate_30s = _get_trade_rate(symbol, window_secs=30)
        _rate_avg = _get_avg_trade_rate(symbol)
        if _rate_avg > 0 and _rate_30s > 0:
            _rate_ratio = _rate_30s / _rate_avg
            if _rate_ratio < 0.5:
                logger.info(
                    f"[LOGIC:UPDOWN:{symbol}] CONVICTION — rate={_rate_30s:.2f}/s "
                    f"avg={_rate_avg:.2f}/s ratio={_rate_ratio:.2f}× (need ≥0.5×) → SKIP (low conviction)"
                )
                return None
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] CONVICTION — rate={_rate_30s:.2f}/s "
                f"avg={_rate_avg:.2f}/s ratio={_rate_ratio:.2f}× → PASS"
            )

        # ── MARKET REGIME FILTER ──────────────────────────────────────────────
        regime = _market_regime(symbol)
        signal_dir = "UP" if window_return > 0 else "DOWN"
        if regime == "CHOPPY":
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} signal={signal_dir} → SKIP (choppy = 50% noise)"
            )
            return None
        if regime == "TRENDING_UP" and signal_dir == "DOWN":
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} contradicts DOWN signal → SKIP"
            )
            return None
        if regime == "TRENDING_DOWN" and signal_dir == "UP":
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} contradicts UP signal → SKIP"
            )
            return None
        logger.debug(f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} compatible with {signal_dir} → PASS")

        # ── MEAN REVERSION CHECK ──────────────────────────────────────────────
        _is_extreme_move = abs(window_return) >= 0.003  # 0.3%
        if _is_extreme_move:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] EXTREME-MOVE — win_ret={window_return:+.4%} ≥ 0.3% "
                f"→ confidence will be capped at MEDIUM unless 3+ confirmations"
            )

        # ── FUNDING RATE CONFIRMATION ─────────────────────────────────────────
        _funding = _get_funding_rate(symbol)
        if _funding is not None:
            _funding_dir = "UP" if _funding > 0 else "DOWN"
            _funding_aligns = _funding_dir == signal_dir
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] FUNDING — rate={_funding:.6f} dir={_funding_dir} "
                f"signal={signal_dir} → {'CONFIRMS' if _funding_aligns else 'CONTRADICTS'} "
                f"{'(soft downgrade to confidence score)' if not _funding_aligns else ''}"
            )
        else:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] FUNDING — no data (neutral)")

        # ── OI DELTA CONFIRMATION ─────────────────────────────────────────────
        _oi_delta = _get_oi_delta(symbol)
        _oi_confirms = _oi_delta is not None and _oi_delta > 0.001
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] OI-DELTA — delta={_oi_delta} "
            f"→ {'CONFIRMS (rising OI = conviction)' if _oi_confirms else 'no confirmation'}"
        )

        # ── CROSS-EXCHANGE DIVERGENCE ─────────────────────────────────────────
        _ce_div = _cross_exchange_divergence(symbol)
        _ce_confirms = abs(_ce_div) > 0.0002 and ((_ce_div > 0) == (window_return > 0))
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] CE-DIV — coinbase_vs_binance={_ce_div:+.4%} "
            f"→ {'CONFIRMS arb pressure' if _ce_confirms else 'no confirmation'}"
        )

        # ── MOMENTUM ACCELERATION CHECK ───────────────────────────────────────
        if secs_in >= 45:
            accel = _price_acceleration(symbol)
            if accel is not None:
                _accel_ok = (accel > 0) == (window_return > 0)
                if not _accel_ok:
                    logger.info(
                        f"[LOGIC:UPDOWN:{symbol}] ACCEL — win_ret={window_return:+.4%} "
                        f"accel={accel:+.4%} DECELERATING at t={secs_in:.0f}s → SKIP (mean reversion likely)"
                    )
                    return None
                logger.debug(
                    f"[LOGIC:UPDOWN:{symbol}] ACCEL — win_ret={window_return:+.4%} "
                    f"accel={accel:+.4%} still accelerating → PASS"
                )
        else:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] ACCEL — t={secs_in:.0f}s < 45s, check skipped")

        # ── MULTI-TIMEFRAME CONSENSUS ─────────────────────────────────────────
        consensus_score, consensus_conf = _multitf_consensus(symbol)
        if consensus_conf not in ("NONE",) and consensus_score != 0.0:
            consensus_dir = "UP" if consensus_score > 0 else "DOWN"
            signal_dir    = "UP" if window_return > 0 else "DOWN"
            if consensus_dir != signal_dir:
                logger.info(
                    f"[LOGIC:UPDOWN:{symbol}] MULTITF — score={consensus_score:+.2f} conf={consensus_conf} "
                    f"consensus={consensus_dir} vs signal={signal_dir} → SKIP (timeframes disagree)"
                )
                return None
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] MULTITF — score={consensus_score:+.2f} conf={consensus_conf} "
                f"consensus={consensus_dir} agrees with signal → PASS"
            )
        else:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] MULTITF — score={consensus_score:+.2f} conf={consensus_conf} "
                f"→ no strong consensus, proceeding"
            )

        # ── CONSECUTIVE WINDOW TREND ──────────────────────────────────────────
        trend_score = _consecutive_window_trend(symbol)  # -1..+1, None = unknown
        trend_dir_ok = True
        _min_trend = float(_ap.get("min_trend_score", 0.0))
        if trend_score is not None:
            trend_direction  = "UP" if trend_score > 0 else "DOWN"
            signal_direction = "UP" if window_return > 0 else "DOWN"
            if trend_direction != signal_direction and abs(trend_score) >= 0.5:
                logger.debug(
                    f"[LOGIC:UPDOWN:{symbol}] CW-TREND — score={trend_score:+.2f} "
                    f"trend={trend_direction} contradicts signal={signal_direction} (|score|≥0.5) → SKIP"
                )
                return None
            trend_dir_ok = trend_direction == signal_direction
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] CW-TREND — score={trend_score:+.2f} "
                f"trend={trend_direction} signal={signal_direction} "
                f"aligned={'YES' if trend_dir_ok else 'NO (weak, not blocking)'}"
            )
        else:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] CW-TREND — no data (neutral, not blocking)")
        if _min_trend > 0 and (trend_score is None or abs(trend_score) < _min_trend):
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] MIN-TREND — score={trend_score} < required {_min_trend} → SKIP"
            )
            return None

        # ── BTC LEADERSHIP ────────────────────────────────────────────────────
        btc_lead = _btc_leadership_signal(symbol)
        combined = window_return + btc_lead
        if btc_lead != 0.0 and (combined > 0) != (window_return > 0):
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] BTC-LEAD — btc={btc_lead:+.4%} "
                f"combined={combined:+.4%} REVERSES signal direction → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] BTC-LEAD — btc={btc_lead:+.4%} "
            f"win_ret={window_return:+.4%} combined={combined:+.4%} → PASS "
            f"direction={'UP' if combined > 0 else 'DOWN'}"
        )

        direction = "UP" if combined > 0 else "DOWN"

        # ── Fair value + edge ─────────────────────────────────────────────────
        fair_prob = compute_updown_fair_value(window_return, trend_score, trend_dir_ok)

        if direction == "UP":
            side = "YES"
            token = market.yes_token
            mkt_price = mid
        else:
            side = "NO"
            token = market.no_token
            mkt_price = 1.0 - mid

        fair_value = float(np.clip(fair_prob, 0.01, 0.99))
        edge = fair_value - mkt_price

        if edge < config.MIN_EDGE:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
                f"edge={edge:+.3f} < MIN_EDGE={config.MIN_EDGE:.3f} → SKIP (insufficient edge)"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
            f"edge={edge:+.3f} ≥ MIN_EDGE={config.MIN_EDGE:.3f} → PASS"
        )

        # ── CONFIDENCE SCORING ────────────────────────────────────────────────
        _confirmations = sum([
            abs(window_return) >= 0.003,                                      # strong move
            trend_score is not None and abs(trend_score) >= 0.75,            # strong trend
            _oi_confirms,                                                      # OI rising
            _ce_confirms,                                                      # cross-exchange confirms
            _funding is not None and ((_funding > 0) == (window_return > 0)), # funding aligns
        ])
        if _is_extreme_move and _confirmations < 3:
            confidence = "MEDIUM"
        elif _confirmations >= 3:
            confidence = "HIGH"
        elif _confirmations >= 1:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] CONFIDENCE — confirmations={_confirmations}/5 "
            f"(strong_move={abs(window_return)>=0.003}, trend={trend_score is not None and abs(trend_score or 0)>=0.75}, "
            f"oi={_oi_confirms}, ce={_ce_confirms}, "
            f"funding={_funding is not None and ((_funding>0)==(window_return>0))}) "
            f"extreme={_is_extreme_move} → {confidence}"
        )
        pressure = _exchange_pressure(symbol)

        # Relative strength: how much does this signal exceed its own threshold?
        # BTC at 0.04% with 0.035% threshold = 1.14×  (stronger relative signal)
        # SOL at 0.04% with 0.080% threshold = 0.50×  (weaker relative signal)
        # This is the sort key used to pick the single best trade per loop.
        rel_strength = abs(window_return) / _sig_thresh if _sig_thresh > 0 else 0.0

        # Consecutive window persistence: if 4+ of last 5 windows went same direction, boost rel_strength
        if trend_score is not None and abs(trend_score) >= 0.6:
            persistence_boost = 1.0 + abs(trend_score) * 0.5  # up to 1.5× boost at score=1.0
            rel_strength *= persistence_boost

        # prefer_assets boost: LLM-preferred assets get higher relative strength so they win the ranking
        if symbol.upper() in [s.upper() for s in _ap.get("prefer_assets", [])]:
            rel_strength *= 1.5
            logger.debug(f"[UPDOWN] {symbol} prefer_assets boost → rel_strength={rel_strength:.2f}×")

        # ── Advanced probability modifiers ────────────────────────────────────
        # 1. Bayesian: boost/penalise based on historical signal accuracy for this asset
        _bayes_mult  = _bayesian_win_rate_mult(symbol)
        # 2. Normal vol: boost if signal is unusually strong vs historical window volatility
        _vol_mult    = _normal_vol_mult(symbol, window_return)
        # 3. Markov: boost if this direction has been persistent for this asset
        _markov_mult = _markov_persistence_mult(symbol, direction)
        _prob_mult   = _bayes_mult * _vol_mult * _markov_mult
        _rel_before  = rel_strength
        rel_strength *= _prob_mult
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] PROB-MULTS — "
            f"bayes={_bayes_mult:.3f}×  vol={_vol_mult:.3f}×  markov={_markov_mult:.3f}×  "
            f"combined={_prob_mult:.3f}×  rel_strength: {_rel_before:.3f} → {rel_strength:.3f}"
        )

        logger.info(
            f"[UPDOWN] {symbol} {direction}  "
            f"win_ret={window_return:+.4%}  thresh={_sig_thresh:.4%}  rel={rel_strength:.2f}×  "
            f"trend={f'{trend_score:+.2f}' if trend_score is not None else 'N/A'}  btc={btc_lead:+.4%}  "
            f"fair={fair_value:.3f}  mkt={mkt_price:.3f}  edge={edge:+.3f}  "
            f"t={secs_in:.0f}s  → {side} [{confidence}]  \"{market.question[:45]}\""
        )

        # Record signal direction in session tracker so we can measure accuracy later
        _st.update(symbol, live_price, signal_direction=direction)

        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_price,
            fair_value=fair_value,
            edge=edge,
            signal=float(np.clip(abs(window_return) * 200, 0.0, 1.0)),
            confidence=confidence,
            momentum_signal=float(window_return),
            imbalance_signal=float(pressure),
            is_latency_arb=False,
            secs_into_window=float(secs_in),
            rel_strength=rel_strength,
            win_mins=window_mins,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 2: TrendFollowStrategy  (per-asset win-streak direction tracker)
# ═══════════════════════════════════════════════════════════════════════════
#
# Track which direction (UP / DOWN) each asset has been winning in across
# successive 5-minute UpDown markets.
#
#   WIN  → keep betting the same direction (trend continuing)
#   LOSS → flip to the opposite direction (trend reversed)
#
# We enter near the START of each new 5-minute window when the price is
# still close to 0.50, before the market has priced in the direction.
# ═══════════════════════════════════════════════════════════════════════════

class TrendFollowStrategy:
    """
    Follows the established per-asset UpDown trend direction.

    State is maintained by a TrendTracker instance (passed in).
    Signals are only generated when:
      - The tracker has history for the asset (direction is known).
      - The market price is still near 0.50 (early in the window).
      - Edge exceeds MIN_EDGE.
    """

    MAX_ENTRY_PRICE = 0.60   # don't chase — only enter when market hasn't moved much
    FAIR_VALUE_BASE = 0.63   # baseline fair value for trend continuation
    STREAK_BONUS    = 0.02   # +2% per streak step, capped at 0.80

    def __init__(self, tracker) -> None:
        self._tracker = tracker

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        symbol = _detect_updown_market(market.question)
        if symbol is None:
            return None

        direction = self._tracker.get_direction(symbol)
        if direction is None:
            logger.debug(f"[LOGIC:TREND:{symbol}] DIRECTION — no history in TrendTracker → SKIP (cold start)")
            return None

        # Strict timing
        secs_in = _market_seconds_into_window(market)
        if secs_in is None or secs_in < 5 or secs_in > 240:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] TIMING — secs_in={secs_in} (need 5–240s) → SKIP"
            )
            return None

        streak = self._tracker.get_streak(symbol)
        mid = order_book.mid if order_book else market.yes_price
        logger.debug(
            f"[LOGIC:TREND:{symbol}] STATE — direction={direction} streak={streak} "
            f"mid={mid:.3f} t={secs_in:.0f}s"
        )

        if direction == "UP":
            side  = "YES"
            token = market.yes_token
            mkt_price = mid
        else:
            side  = "NO"
            token = market.no_token
            mkt_price = 1.0 - mid

        # Price guard: only enter near 0.50
        if mkt_price > self.MAX_ENTRY_PRICE:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] PRICE-GUARD — mkt_price={mkt_price:.3f} "
                f"> MAX={self.MAX_ENTRY_PRICE} (market moved, too late) → SKIP"
            )
            return None

        # Book liquidity guard: bid=0.01/ask=0.99 means no real market — mid=0.50
        # is a fake average of empty quotes, not a tradeable price. ASK-GUARD in
        # execute_signal would catch this later but checking here avoids wasted
        # signal computation (HYPE always has an empty book, for example).
        if order_book and order_book.best_bid > 0 and order_book.best_ask > 0:
            _spread = order_book.best_ask - order_book.best_bid
            if _spread > 0.85:
                logger.debug(
                    f"[LOGIC:TREND:{symbol}] BOOK-EMPTY — bid={order_book.best_bid:.3f} "
                    f"ask={order_book.best_ask:.3f} spread={_spread:.3f} → SKIP (no liquidity)"
                )
                return None

        logger.debug(
            f"[LOGIC:TREND:{symbol}] PRICE-GUARD — mkt_price={mkt_price:.3f} "
            f"≤ MAX={self.MAX_ENTRY_PRICE} → PASS"
        )

        # Window-relative confirmation: current Binance move must agree with streak direction
        window_return = _window_return(symbol, int(secs_in))
        if window_return is None:
            logger.debug(f"[LOGIC:TREND:{symbol}] WINDOW-CONFIRM — window_return unavailable → SKIP")
            return None
        window_dir = "UP" if window_return > 0 else "DOWN"
        if window_dir != direction:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] WINDOW-CONFIRM — win_ret={window_return:+.4%} "
                f"dir={window_dir} contradicts streak direction={direction} → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:TREND:{symbol}] WINDOW-CONFIRM — win_ret={window_return:+.4%} "
            f"dir={window_dir} agrees with streak={direction} → PASS"
        )

        btc_lead = _btc_leadership_signal(symbol)
        combined = window_return + btc_lead
        logger.debug(
            f"[LOGIC:TREND:{symbol}] BTC-LEAD — btc={btc_lead:+.4%} "
            f"win_ret={window_return:+.4%} combined={combined:+.4%}"
        )

        # Consecutive-window trend boost
        cw_trend = _consecutive_window_trend(symbol)
        cw_boost = 0
        if cw_trend is not None:
            cw_dir = "UP" if cw_trend > 0 else "DOWN"
            if cw_dir == direction and abs(cw_trend) >= 0.5:
                cw_boost = 1
        logger.debug(
            f"[LOGIC:TREND:{symbol}] CW-TREND — score={cw_trend} boost={cw_boost} "
            f"(+1 to streak if recent windows align)"
        )

        # Fair value + edge
        fair_value = compute_trendfollow_fair_value(streak, cw_boost)
        edge = fair_value - mkt_price
        if edge < config.MIN_EDGE:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
                f"edge={edge:+.3f} < MIN_EDGE={config.MIN_EDGE:.3f} → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:TREND:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
            f"edge={edge:+.3f} ≥ MIN_EDGE={config.MIN_EDGE:.3f} → PASS"
        )

        # Binomial significance: P(k consecutive wins | p=0.5) = 0.5^k
        confidence = _binomial_streak_confidence(streak)
        _p_luck = 0.5 ** streak
        logger.debug(
            f"[LOGIC:TREND:{symbol}] BINOMIAL — streak={streak} "
            f"P(luck)={_p_luck:.4f} → confidence={confidence}"
        )
        if confidence == "LOW" and abs(window_return) < _MIN_WINDOW_RETURN_PCT * 2:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] LOW-CONF-GATE — LOW confidence + weak win_ret={window_return:+.4%} "
                f"(need ≥{_MIN_WINDOW_RETURN_PCT*2:.4%}) → SKIP (too risky on fresh flip)"
            )
            return None

        # Probability multipliers (Bayesian + Markov; no Normal-vol for trend strategy)
        _tf_bayes  = _bayesian_win_rate_mult(symbol)
        _tf_markov = _markov_persistence_mult(symbol, direction)
        _tf_prob   = _tf_bayes * _tf_markov
        _tf_rel_strength = (edge / config.MIN_EDGE) * (1.0 + streak * 0.1) * _tf_prob
        logger.debug(
            f"[LOGIC:TREND:{symbol}] PROB-MULTS — "
            f"bayes={_tf_bayes:.3f}×  markov={_tf_markov:.3f}×  combined={_tf_prob:.3f}×  "
            f"rel_strength={_tf_rel_strength:.3f} "
            f"(base={edge/config.MIN_EDGE:.2f}× × streak_factor={1.0+streak*0.1:.2f}× × prob={_tf_prob:.3f}×)"
        )

        _cw_str = f"{cw_trend:+.2f}" if cw_trend is not None else "N/A"
        logger.info(
            f"[TREND-FOLLOW] {symbol} {direction}  streak={streak}  cw={_cw_str}  "
            f"win_ret={window_return:+.4%}  "
            f"fair={fair_value:.2f}  mkt={mkt_price:.2f}  edge={edge:+.2f}  "
            f"bayes={_tf_bayes:.2f}×  markov={_tf_markov:.2f}×  "
            f"t={secs_in:.0f}s  [{confidence}]  \"{market.question[:45]}\""
        )

        _st.update(symbol, live_price, signal_direction=direction)
        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_price,
            fair_value=fair_value,
            edge=edge,
            signal=edge,
            confidence=confidence,
            momentum_signal=float(streak),
            imbalance_signal=float(combined),
            is_latency_arb=False,
            rel_strength=_tf_rel_strength,
        )
