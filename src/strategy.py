"""
Trading strategies for Polymarket — UpDown crypto markets only.

Two strategies:
  1. TrendFollowStrategy  — follow per-asset win streaks + momentum confirm
  2. UpDownMomentumStrategy — Binance multi-timeframe momentum (cold start)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import requests
from loguru import logger

from src.client import Market, OrderBook
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
    is_news_arb: bool = False

    def __str__(self) -> str:
        return (
            f"[{self.confidence}] {self.side} {self.question[:60]} | "
            f"mkt={self.market_price:.3f}  fv={self.fair_value:.3f}  "
            f"edge={self.edge:+.3f}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Price feed infrastructure (Binance + CoinGecko fallback)
# ═══════════════════════════════════════════════════════════════════════════

# Binance is the fastest public feed; CoinGecko is fallback (rate-limited)
_BINANCE_FEEDS: dict[str, str] = {
    "BTC":  "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
    "ETH":  "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
    "XRP":  "https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT",
    "SOL":  "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
    "DOGE": "https://api.binance.com/api/v3/ticker/price?symbol=DOGEUSDT",
}
_COINGECKO_IDS: dict[str, str] = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "XRP":  "ripple",
    "SOL":  "solana",
    "DOGE": "dogecoin",
}

_PRICE_CACHE: dict[str, tuple[float, float]] = {}     # symbol → (price, timestamp)
_EXCHANGE_PRESSURE: dict[str, float] = {}            # symbol → bid/ask imbalance (-1..+1)
_CACHE_TTL = 2.0   # seconds

# Rolling 60-second price history for momentum: symbol → [(price, ts), ...]
_PRICE_HISTORY: dict[str, list[tuple[float, float]]] = {}
_HISTORY_WINDOW = 600  # keep 10 minutes of ticks for multi-timeframe analysis


def _fetch_price(symbol: str) -> float | None:
    """Fetch live price for any supported symbol (cached 2 s)."""
    sym = symbol.upper()
    cached = _PRICE_CACHE.get(sym)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]

    price: float | None = None

    # Try Binance first (fastest, no rate limit for single ticker)
    if sym in _BINANCE_FEEDS:
        try:
            r = requests.get(_BINANCE_FEEDS[sym], timeout=3)
            r.raise_for_status()
            price = float(r.json()["price"])
        except Exception:
            pass

    # Fall back to CoinGecko
    if price is None and sym in _COINGECKO_IDS:
        try:
            cg_id = _COINGECKO_IDS[sym]
            r = requests.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd",
                timeout=4,
            )
            r.raise_for_status()
            price = float(r.json()[cg_id]["usd"])
        except Exception:
            pass

    if price is not None:
        _PRICE_CACHE[sym] = (price, time.time())
        # Record in rolling history
        hist = _PRICE_HISTORY.setdefault(sym, [])
        now = time.time()
        hist.append((price, now))
        # Trim to window
        cutoff = now - _HISTORY_WINDOW
        _PRICE_HISTORY[sym] = [(p, t) for p, t in hist if t >= cutoff]

    return price


def _fetch_btc_price() -> float | None:
    """Backward-compatible wrapper."""
    return _fetch_price("BTC")


def _price_momentum(symbol: str, window_secs: int) -> float | None:
    """
    Returns the % price change over the last `window_secs` seconds.
    Positive = rising, negative = falling. None if insufficient history.
    """
    hist = _PRICE_HISTORY.get(symbol.upper(), [])
    now = time.time()
    window = [(p, t) for p, t in hist if now - t <= window_secs * 1.25]
    if len(window) < 2:
        return None
    oldest_price = window[0][0]
    newest_price = window[-1][0]
    if oldest_price <= 0:
        return None
    return (newest_price - oldest_price) / oldest_price


def _price_momentum_60s(symbol: str) -> float | None:
    return _price_momentum(symbol, 60)


def _price_momentum_30s(symbol: str) -> float | None:
    return _price_momentum(symbol, 30)


def _price_momentum_5m(symbol: str) -> float | None:
    return _price_momentum(symbol, 300)


def _price_momentum_15s(symbol: str) -> float | None:
    return _price_momentum(symbol, 15)


def _window_return(symbol: str, secs_in: int) -> float | None:
    """
    Price change from exactly secs_in seconds ago (≈ window open) to now.
    Uses nearest-tick lookup so we don't accidentally grab pre-window prices.
    Returns None if no tick within 25 seconds of the target time.
    """
    sym = symbol.upper()
    hist = _PRICE_HISTORY.get(sym, [])
    if len(hist) < 2:
        return None
    now = time.time()
    target = now - secs_in
    # Find the tick whose timestamp is closest to the window-start reference
    best = min(hist, key=lambda x: abs(x[1] - target))
    if abs(best[1] - target) > 25:   # reject if off by more than 25s
        return None
    ref_price = best[0]
    cur_price = hist[-1][0]
    if ref_price <= 0:
        return None
    return (cur_price - ref_price) / ref_price


def _consecutive_window_trend(symbol: str, n_windows: int = 4) -> float | None:
    """
    Looks at the last n_windows completed 5-minute windows and returns
    a score from -1.0 to +1.0:
      +1.0 = all windows closed UP (strong upward trend)
      -1.0 = all windows closed DOWN (strong downward trend)
       0.0 = mixed (no trend)

    Uses price history ticks. Requires at least 2 valid window samples.
    """
    sym = symbol.upper()
    hist = _PRICE_HISTORY.get(sym, [])
    if len(hist) < 10:
        return None
    now = time.time()
    results = []
    for i in range(1, n_windows + 1):
        # Each completed window: ends i*300s ago, starts (i+1)*300s ago
        end_target   = now - i * 300
        start_target = now - (i + 1) * 300
        end_tick   = min(hist, key=lambda x: abs(x[1] - end_target))
        start_tick = min(hist, key=lambda x: abs(x[1] - start_target))
        # Only use if we have ticks within 60s of the target time
        if abs(end_tick[1] - end_target) > 60 or abs(start_tick[1] - start_target) > 60:
            continue
        if start_tick[0] <= 0:
            continue
        window_ret = (end_tick[0] - start_tick[0]) / start_tick[0]
        results.append(1 if window_ret > 0 else -1)
    if len(results) < 2:
        return None
    return sum(results) / len(results)   # -1.0 .. +1.0


# Assets that BTC leads (moves before them in correlated markets)
_BTC_LED_ALTS = frozenset({"ETH", "SOL", "XRP", "DOGE", "BNB"})


def _btc_leadership_signal(symbol: str) -> float:
    """
    For alt coins: BTC moves first, alts follow with a 15-45s lag.
    Returns a directional nudge (same sign = agree with BTC direction).
    Returns 0.0 for BTC or unknown symbols.
    """
    if symbol.upper() not in _BTC_LED_ALTS:
        return 0.0
    btc_15s = _price_momentum_15s("BTC")
    btc_30s = _price_momentum_30s("BTC")
    if btc_15s is None and btc_30s is None:
        return 0.0
    # Prefer very recent BTC signal; scale down (BTC signal is imperfect for alts)
    sig = btc_15s if btc_15s is not None else btc_30s
    return sig * 0.4   # 40% weight — confirms but doesn't override


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


def _price_acceleration(symbol: str) -> float | None:
    """
    Returns how much momentum is changing (2nd derivative of price).
    Positive = trend is speeding up, negative = trend is slowing/reversing.
    Computed as: mom_30s - mom_60s (scaled to same units).
    Returns None if insufficient data.
    """
    m30 = _price_momentum_30s(symbol)
    m60 = _price_momentum_60s(symbol)
    if m30 is None or m60 is None:
        return None
    # Annualise to same time scale: m30 is over 30s, m60 over 60s
    # Acceleration = recent 30s rate vs older 60s rate
    m30_scaled = m30 * 2.0   # scale 30s → 60s equivalent
    return m30_scaled - m60


def _exchange_pressure(symbol: str) -> float:
    """
    Returns Binance bid/ask size imbalance for `symbol`.
    Range: -1 (heavy ask pressure, likely falling) to +1 (heavy bid pressure, likely rising).
    0.0 if no data available.
    """
    return _EXCHANGE_PRESSURE.get(symbol.upper(), 0.0)


def _multitf_consensus(symbol: str) -> tuple[float, str]:
    """
    Combines 30s, 60s, 5m momentum + exchange pressure into a single
    directional score and confidence.

    Returns: (direction_score, confidence)
      direction_score: positive = UP, negative = DOWN, magnitude = strength
      confidence: "HIGH" | "MEDIUM" | "LOW" | "NONE" (insufficient data)
    """
    sym = symbol.upper()
    hist = _PRICE_HISTORY.get(sym, [])

    # Require at least 5 distinct ticks AND 90 seconds of real price history.
    # With fewer ticks, all three timeframes return the same value (same 1-2 points
    # fall in every window) — this looks like strong agreement but is just noise.
    if len(hist) < 5:
        return 0.0, "NONE"
    time_span = hist[-1][1] - hist[0][1]
    if time_span < 90:
        return 0.0, "NONE"

    m30 = _price_momentum_30s(sym)
    m60 = _price_momentum_60s(sym)
    m5m = _price_momentum_5m(sym)
    pressure = _exchange_pressure(sym)
    accel = _price_acceleration(sym)

    available = sum(x is not None for x in [m30, m60, m5m])
    if available < 2:
        return 0.0, "NONE"

    # Reject signals where all available timeframes are identical — that means
    # only one distinct price exists in the history window (still warming up).
    vals = [x for x in [m30, m60, m5m] if x is not None]
    if len(vals) >= 2 and len(set(round(v, 8) for v in vals)) == 1:
        return 0.0, "NONE"

    signals = []
    if m30 is not None:
        signals.append(m30 * 2.0)     # 30s scaled to 60s units, weight 1.0×
    if m60 is not None:
        signals.append(m60 * 1.0)     # 60s baseline, weight 1.0×
    if m5m is not None:
        signals.append(m5m * 0.2)     # 5m smoothed, weight 0.2× (directional only)
    if pressure != 0.0:
        signals.append(pressure * 0.003)  # convert to % units

    consensus = sum(signals) / len(signals)

    # Check cross-timeframe agreement — all pointing same direction = higher confidence
    directions = []
    if m30 is not None:
        directions.append(1 if m30 > 0 else -1)
    if m60 is not None:
        directions.append(1 if m60 > 0 else -1)
    if m5m is not None:
        directions.append(1 if m5m > 0 else -1)
    if pressure != 0.0:
        directions.append(1 if pressure > 0.1 else (-1 if pressure < -0.1 else 0))

    n_agree = sum(d == (1 if consensus > 0 else -1) for d in directions if d != 0)
    n_total = sum(d != 0 for d in directions)
    agreement_ratio = n_agree / n_total if n_total > 0 else 0.5

    # Acceleration bonus: trend speeding up = more conviction
    accel_bonus = 0.0
    if accel is not None and (accel > 0) == (consensus > 0):
        accel_bonus = min(abs(accel) * 0.3, 0.002)

    strength = abs(consensus) + accel_bonus

    if agreement_ratio >= 0.75 and strength >= 0.004:
        conf = "HIGH"
    elif agreement_ratio >= 0.60 and strength >= 0.003:
        conf = "MEDIUM"
    elif agreement_ratio >= 0.50 and strength >= 0.002:
        conf = "LOW"
    else:
        conf = "NONE"

    return consensus, conf



# ═══════════════════════════════════════════════════════════════════════════
# UpDown market detection helpers
# ═══════════════════════════════════════════════════════════════════════════

import re as _re

# Maps keywords in the market question to the Binance symbol to fetch
_UPDOWN_ASSETS = {
    "xrp":      "XRP",
    "ripple":   "XRP",
    "btc":      "BTC",
    "bitcoin":  "BTC",
    "eth":      "ETH",
    "ethereum": "ETH",
    "sol":      "SOL",
    "solana":   "SOL",
    "doge":     "DOGE",
    "dogecoin": "DOGE",
    "bnb":      "BNB",
    "hype":     "HYPE",
}

# Minimum absolute 60s momentum to act on (0.05% move in 60s)
_MIN_MOMENTUM_PCT = 0.0005

# Minimum window-relative return to generate a signal.
# 0.08% means BTC moved 0.08% from window-start — enough edge to trade.
# At 30s into a 5-min window, a 0.08% move gives ~60% probability of
# staying in the same direction at resolution (Brownian bridge estimate).
_MIN_WINDOW_RETURN_PCT = 0.0008


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
        if secs_in is None or secs_in < 5 or secs_in > _max_secs_in:
            return None

        # Time-of-day skip (UTC hours the LLM decided are bad)
        _tod_skip = _ap.get("time_of_day_skip", [])
        if _tod_skip and time.gmtime().tm_hour in _tod_skip:
            return None

        # Warm up price history — require minimum seconds of feed data
        _min_hist = int(_ap.get("min_price_history_s", 120))
        live_price = _fetch_price(symbol)
        if live_price is None:
            return None
        hist = _PRICE_HISTORY.get(symbol.upper(), [])
        if len(hist) < 2:
            return None
        hist_span = hist[-1][1] - hist[0][1]
        if hist_span < _min_hist:
            return None

        # ── Guard 2: Entry price must still be near 0.50 ─────────────────────
        mid = order_book.mid if order_book else market.yes_price
        if mid > 0.62 or mid < 0.38:
            return None

        # ── PRIMARY SIGNAL: window-relative return ────────────────────────────
        # _price_momentum(symbol, secs_in) = (price_now - price_at_window_start)
        #                                    / price_at_window_start
        # This is EXACTLY what the oracle measures. If it's positive → YES is
        # already ahead; negative → NO is already ahead. The Polymarket price
        # is still 0.50 (oracle lag) → we have edge.
        # Apply LLM-suggested skip list
        if symbol.upper() in [s.upper() for s in _ap.get("skip_assets", [])]:
            logger.debug(f"[UPDOWN SKIP] {symbol} on analyst skip list")
            return None

        window_return = _window_return(symbol, int(secs_in))
        # Per-asset threshold overrides take priority over global threshold
        _asset_thresholds = _ap.get("asset_thresholds", {})
        _sig_thresh = float(
            _asset_thresholds.get(symbol.upper(),
            _ap.get("signal_threshold", _MIN_WINDOW_RETURN_PCT))
        )
        if window_return is None or abs(window_return) < _sig_thresh:
            logger.debug(
                f"[UPDOWN SKIP] {symbol} win_ret={window_return}  "
                f"min={_sig_thresh:.4%}  t={secs_in:.0f}s"
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
                logger.debug(f"[UPDOWN SKIP] {symbol} trend={trend_score:+.2f} contradicts window_return")
                return None
            trend_dir_ok = trend_direction == signal_direction
        # If analyst requires minimum trend alignment, enforce it
        if _min_trend > 0 and (trend_score is None or abs(trend_score) < _min_trend):
            logger.debug(f"[UPDOWN SKIP] {symbol} trend={trend_score} below min={_min_trend:.2f}")
            return None

        # ── CONFIRMATION 2: BTC leadership for alts ──────────────────────────
        btc_lead = _btc_leadership_signal(symbol)
        combined = window_return + btc_lead

        # Skip if BTC leadership strongly contradicts the window return
        if btc_lead != 0.0 and (combined > 0) != (window_return > 0):
            logger.debug(f"[UPDOWN SKIP] {symbol} BTC lead contradicts window return")
            return None

        direction = "UP" if combined > 0 else "DOWN"

        # ── Fair value ────────────────────────────────────────────────────────
        strength = min(abs(window_return) / 0.01, 1.0)
        # Base 53% → 68%; add up to +5% when trend agrees (consecutive windows)
        trend_boost = 0.05 * abs(trend_score) if (trend_score is not None and trend_dir_ok) else 0.0
        fair_prob = 0.53 + strength * 0.15 + trend_boost
        fair_prob = float(np.clip(fair_prob, 0.53, 0.73))

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
            f"win_ret={window_return:+.4%}  trend={trend_score:+.2f if trend_score is not None else 'N/A'}  btc={btc_lead:+.4%}  "
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
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 3b: Trend Follow  (per-asset win-streak direction tracker)
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

        fair_value = min(self.FAIR_VALUE_BASE + (streak + cw_boost) * self.STREAK_BONUS, 0.72)
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


