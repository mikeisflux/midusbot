"""
Trading strategies for Polymarket — UpDown crypto markets only.

Two strategies:
  1. TrendFollowStrategy  — follow per-asset win streaks + momentum confirm
  2. UpDownMomentumStrategy — Binance multi-timeframe momentum (cold start)

Price-feed infrastructure lives in src.signals.
Fair-value computation lives in src.fair_value.
"""
from __future__ import annotations

import re as _re
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from loguru import logger

from src.client import Market, OrderBook
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

# Per-asset default thresholds based on typical 5-min volatility.
# These seed the system before the learner/analyst have enough data.
# BTC/ETH/BNB are slow large-caps; SOL/DOGE/HYPE move faster.
# Analyst and learner can raise or lower these at any time.
_DEFAULT_ASSET_THRESHOLDS: dict[str, float] = {
    "BTC":  0.0003,   # ~$20 move on $67k — slow mover
    "ETH":  0.0003,   # ~$0.60 move on $2050
    "BNB":  0.0003,   # ~$0.18 move on $589
    "XRP":  0.0003,   # ~$0.0004 move on $1.32
    "SOL":  0.0004,   # more volatile than large-caps
    "DOGE": 0.0004,   # small price, high % swings
    "HYPE": 0.0005,   # erratic/newer asset, needs cleaner signal
}


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

        # ── Guard 1: Strict timing — must know window start, 15–75s in ───────
        # The oracle lag edge is only available in the first ~75s of each window.
        # Before 15s we have too little window data to measure the return.
        # After 75s the market makers have already repriced to fair value.
        # If end_date is unparseable, skip entirely — don't trade blind.
        secs_in = _market_seconds_into_window(market)
        # Load analyst params (LLM-tuned)
        try:
            from src.analyst import load_params as _load_analyst_params
            _ap = _load_analyst_params()
        except Exception:
            _ap = {}

        _max_secs_in = int(_ap.get("max_secs_in", 240))
        if secs_in is None or secs_in < 5:
            return None
        if secs_in > _max_secs_in:
            logger.debug(f"[UPDOWN] {symbol} skipped — {secs_in:.0f}s into window (max {_max_secs_in}s)")
            return None

        # Time-of-day skip (UTC hours the LLM decided are bad)
        _tod_skip = _ap.get("time_of_day_skip", [])
        if _tod_skip and time.gmtime().tm_hour in _tod_skip:
            logger.debug(f"[UPDOWN] {symbol} skipped — time-of-day block (UTC hour {time.gmtime().tm_hour})")
            return None

        # Warm up price history — require minimum seconds of feed data
        _min_hist = int(_ap.get("min_price_history_s", 30))   # 30s default (was 120 — far too long)
        live_price = _fetch_price(symbol)
        if live_price is None:
            logger.debug(f"[UPDOWN] {symbol} skipped — no live price from feed")
            return None
        hist = _PRICE_HISTORY.get(symbol.upper(), [])
        if len(hist) < 2:
            logger.debug(f"[UPDOWN] {symbol} skipped — price history empty")
            return None
        hist_span = hist[-1][1] - hist[0][1]
        if hist_span < _min_hist:
            logger.debug(f"[UPDOWN] {symbol} skipped — only {hist_span:.0f}s of price history (need {_min_hist}s)")
            return None

        # ── Guard 2: Entry price must still be near 0.50 ─────────────────────
        mid = order_book.mid if order_book else market.yes_price
        if mid > 0.62 or mid < 0.38:
            logger.debug(f"[UPDOWN] {symbol} skipped — mid={mid:.3f} already priced away from 50/50")
            return None

        # ── PRIMARY SIGNAL: window-relative return ────────────────────────────
        # _price_momentum(symbol, secs_in) = (price_now - price_at_window_start)
        #                                    / price_at_window_start
        # This is EXACTLY what the oracle measures. If it's positive → YES is
        # already ahead; negative → NO is already ahead. The Polymarket price
        # is still 0.50 (oracle lag) → we have edge.
        # Apply LLM-suggested skip list
        if symbol.upper() in [s.upper() for s in _ap.get("skip_assets", [])]:
            return None

        # Use the oldest available price tick as reference if the window opened
        # before the bot started (common after restarts). We cap secs_in to what
        # we actually have so _window_return finds a tick within tolerance.
        with _PRICE_LOCK:
            _hist_now = list(_PRICE_HISTORY.get(symbol.upper(), []))
        if len(_hist_now) >= 2:
            _available_span = _hist_now[-1][1] - _hist_now[0][1]
            _effective_secs = min(int(secs_in), max(15, int(_available_span) - 2))
        else:
            _effective_secs = int(secs_in)

        window_return = _window_return(symbol, _effective_secs)
        # Threshold priority (highest → lowest):
        #   1. analyst/learner per-asset override  (asset_thresholds["BTC"])
        #   2. per-asset default                   (_DEFAULT_ASSET_THRESHOLDS["BTC"])
        #   3. analyst global signal_threshold      (floored at _MIN_WINDOW_RETURN_PCT)
        _asset_thresholds = _ap.get("asset_thresholds", {})
        _sym = symbol.upper()
        if _sym in _asset_thresholds:
            # Explicit per-asset value — respect it directly (tiny sanity floor only)
            _sig_thresh = float(max(0.0001, _asset_thresholds[_sym]))
        else:
            # Fall back to per-asset default or global, whichever is available
            _global = float(max(
                _MIN_WINDOW_RETURN_PCT,
                _ap.get("signal_threshold", _MIN_WINDOW_RETURN_PCT),
            ))
            _sig_thresh = float(_DEFAULT_ASSET_THRESHOLDS.get(_sym, _global))
        if window_return is None:
            logger.debug(f"[UPDOWN] {symbol} skipped — window_return unavailable (lookback={_effective_secs}s, hist={int(_available_span if len(_hist_now)>=2 else 0)}s)")
            return None
        if abs(window_return) < _sig_thresh:
            logger.debug(f"[UPDOWN] {symbol} skipped — win_ret={window_return:+.4%} below thresh {_sig_thresh:.4%}")
            return None

        # ── CONFIRMATION 0: momentum must be accelerating (not decelerating) ──
        # If the trend is slowing down 45+ seconds in, mean reversion is likely.
        # We skip — a fading move at 0.25% is still likely to revert by resolution.
        if secs_in >= 45:
            accel = _price_acceleration(symbol)
            if accel is not None and (accel > 0) != (window_return > 0):
                logger.debug(
                    f"[UPDOWN] {symbol} skipped — momentum decelerating "
                    f"(win_ret={window_return:+.4%}, accel={accel:+.4%})"
                )
                return None

        # ── CONFIRMATION 0b: multi-timeframe consensus must agree ─────────────
        # Require at least MEDIUM consensus from 30s/60s/5m timeframes.
        # If all timeframes agree the direction is opposite, skip.
        consensus_score, consensus_conf = _multitf_consensus(symbol)
        if consensus_conf not in ("NONE",) and consensus_score != 0.0:
            consensus_dir = "UP" if consensus_score > 0 else "DOWN"
            signal_dir    = "UP" if window_return > 0 else "DOWN"
            if consensus_dir != signal_dir:
                logger.debug(
                    f"[UPDOWN] {symbol} skipped — multitf consensus {consensus_dir} "
                    f"contradicts signal {signal_dir} (conf={consensus_conf})"
                )
                return None

        # ── CONFIRMATION 1: consecutive window trend ─────────────────────────
        trend_score = _consecutive_window_trend(symbol)  # -1..+1, None = unknown
        trend_dir_ok = True
        _min_trend = float(_ap.get("min_trend_score", 0.0))
        if trend_score is not None:
            trend_direction = "UP" if trend_score > 0 else "DOWN"
            signal_direction = "UP" if window_return > 0 else "DOWN"
            if trend_direction != signal_direction and abs(trend_score) >= 0.5:
                return None
            trend_dir_ok = trend_direction == signal_direction
        if _min_trend > 0 and (trend_score is None or abs(trend_score) < _min_trend):
            return None

        # ── CONFIRMATION 2: BTC leadership for alts ──────────────────────────
        btc_lead = _btc_leadership_signal(symbol)
        combined = window_return + btc_lead

        if btc_lead != 0.0 and (combined > 0) != (window_return > 0):
            return None

        direction = "UP" if combined > 0 else "DOWN"

        # ── Fair value ────────────────────────────────────────────────────────
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
            return None

        # HIGH if strong window return OR trend strongly agrees
        confidence = (
            "HIGH" if abs(window_return) >= 0.003 or (trend_score is not None and abs(trend_score) >= 0.75)
            else "MEDIUM"
        )
        pressure = _exchange_pressure(symbol)

        logger.info(
            f"[UPDOWN] {symbol} {direction}  "
            f"win_ret={window_return:+.4%}  trend={f'{trend_score:+.2f}' if trend_score is not None else 'N/A'}  btc={btc_lead:+.4%}  "
            f"fair={fair_value:.3f}  mkt={mkt_price:.3f}  edge={edge:+.3f}  "
            f"t={secs_in:.0f}s  → {side} [{confidence}]  \"{market.question[:45]}\""
        )

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
            return None   # no history yet — UpDownMomentumStrategy handles cold starts

        # Strict timing: same window as UpDownMomentumStrategy
        secs_in = _market_seconds_into_window(market)
        if secs_in is None or secs_in < 5 or secs_in > 240:
            return None

        streak = self._tracker.get_streak(symbol)
        mid = order_book.mid if order_book else market.yes_price

        if direction == "UP":
            side  = "YES"
            token = market.yes_token
            mkt_price = mid
        else:
            side  = "NO"
            token = market.no_token
            mkt_price = 1.0 - mid

        # Price must still be near 0.50 — if it's moved, we're too late
        if mkt_price > self.MAX_ENTRY_PRICE:
            return None

        # Window-relative confirmation: current price must agree with trend direction.
        window_return = _window_return(symbol, int(secs_in))
        if window_return is None:
            return None
        window_dir = "UP" if window_return > 0 else "DOWN"
        if window_dir != direction:
            return None  # window-relative signal contradicts trend — sit out
        combined = window_return + _btc_leadership_signal(symbol)

        # Consecutive-window trend: if recent windows have been going the same
        # way as our streak direction, boost the streak bonus slightly.
        cw_trend = _consecutive_window_trend(symbol)
        cw_boost = 0
        if cw_trend is not None:
            cw_dir = "UP" if cw_trend > 0 else "DOWN"
            if cw_dir == direction and abs(cw_trend) >= 0.5:
                cw_boost = 1  # treat as +1 to streak for fair value calc

        fair_value = compute_trendfollow_fair_value(streak, cw_boost)
        edge = fair_value - mkt_price

        if edge < config.MIN_EDGE:
            return None

        # Streak 1 = LOW, require meaningful window return to trade LOW streaks
        confidence = "HIGH" if streak >= 3 else "MEDIUM" if streak >= 2 else "LOW"
        if confidence == "LOW" and abs(window_return) < _MIN_WINDOW_RETURN_PCT * 2:
            return None   # fresh direction flip + weak window signal — too risky

        logger.info(
            f"[TREND-FOLLOW] {symbol} {direction}  streak={streak}  cw={cw_trend:+.2f}  "
            f"win_ret={window_return:+.4%}  "
            f"fair={fair_value:.2f}  mkt={mkt_price:.2f}  edge={edge:+.2f}  "
            f"t={secs_in:.0f}s  [{confidence}]  \"{market.question[:45]}\""
        )

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
        )
