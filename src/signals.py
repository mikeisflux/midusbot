"""
Price feed infrastructure for UpDown crypto strategies.

Provides:
  - Binance + CoinGecko price feeds with local caching
  - Rolling price history for momentum calculations
  - window_return, price_momentum, consecutive_window_trend helpers
  - Oracle lag / BTC-leadership signal utilities
  - Exchange pressure (bid/ask imbalance) accessors
  - Multi-timeframe consensus scoring
"""
from __future__ import annotations

import threading as _threading
import time
from collections import deque as _deque

import requests
from loguru import logger


# ---------------------------------------------------------------------------
# Feed URL maps
# ---------------------------------------------------------------------------

# Binance is the fastest public feed; CoinGecko is fallback (rate-limited)
_BINANCE_FEEDS: dict[str, str] = {
    "BTC":  "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
    "ETH":  "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
    "XRP":  "https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT",
    "SOL":  "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
    "DOGE": "https://api.binance.com/api/v3/ticker/price?symbol=DOGEUSDT",
    "BNB":  "https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT",
    "HYPE": "https://api.binance.com/api/v3/ticker/price?symbol=HYPEUSDT",
    "AVAX": "https://api.binance.com/api/v3/ticker/price?symbol=AVAXUSDT",
    "LINK": "https://api.binance.com/api/v3/ticker/price?symbol=LINKUSDT",
    "ADA":  "https://api.binance.com/api/v3/ticker/price?symbol=ADAUSDT",
    "LTC":  "https://api.binance.com/api/v3/ticker/price?symbol=LTCUSDT",
    "DOT":  "https://api.binance.com/api/v3/ticker/price?symbol=DOTUSDT",
    "MATIC":"https://api.binance.com/api/v3/ticker/price?symbol=MATICUSDT",
    "SUI":  "https://api.binance.com/api/v3/ticker/price?symbol=SUIUSDT",
    "PEPE": "https://api.binance.com/api/v3/ticker/price?symbol=PEPEUSDT",
    "WIF":  "https://api.binance.com/api/v3/ticker/price?symbol=WIFUSDT",
    "TRX":  "https://api.binance.com/api/v3/ticker/price?symbol=TRXUSDT",
}
_COINGECKO_IDS: dict[str, str] = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "XRP":  "ripple",
    "SOL":  "solana",
    "DOGE": "dogecoin",
    "BNB":  "binancecoin",
    "HYPE": "hyperliquid",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "ADA":  "cardano",
    "LTC":  "litecoin",
    "DOT":  "polkadot",
    "MATIC":"matic-network",
    "SUI":  "sui",
    "PEPE": "pepe",
    "WIF":  "dogwifcoin",
    "TRX":  "tron",
}

# ---------------------------------------------------------------------------
# Shared price state (written by BinanceWSFeed thread, read by main loop)
# ---------------------------------------------------------------------------

_PRICE_CACHE: dict[str, tuple[float, float]] = {}     # symbol → (price, timestamp)
_EXCHANGE_PRESSURE: dict[str, float] = {}            # symbol → bid/ask imbalance (-1..+1)
_CACHE_TTL       = 2.0   # seconds — fresh price window (WS keeps this hot)
_CACHE_TTL_STALE = 30.0  # seconds — stale-but-usable fallback when WS/REST fail
# NOTE: 5-min UpDown oracle lag edge exists only in the first ~90s of a window.
# A 60s stale price would consume 2/3 of the edge window before trading. 30s is
# the upper bound — if price is older than this, skip rather than trade on fiction.

# Feed health: track last WS tick time per symbol for diagnostics
_LAST_WS_TICK: dict[str, float] = {}  # symbol → timestamp of last WS aggTrade

# Rolling 60-second price history for momentum: symbol → [(price, ts), ...]
_PRICE_HISTORY: dict[str, list[tuple[float, float]]] = {}
_HISTORY_WINDOW = 5400  # keep 90 minutes of ticks — supports 12+ completed 5-min windows

# Lock protecting all price globals — written by BinanceWSFeed thread, read by main loop
_PRICE_LOCK = _threading.RLock()


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

def _fetch_price(symbol: str) -> float | None:
    """
    Fetch live price for any supported symbol.

    Priority:
      1. In-process cache (written by BinanceWSFeed WS thread) — if < 2s old
      2. Binance REST fallback — single ticker, no rate limit
      3. CoinGecko REST fallback — slower, rate-limited
      4. Stale cache — if < 60s old, use last known price rather than returning None
         (handles low-volume assets like HYPE whose WS ticks are infrequent)
    """
    sym = symbol.upper()
    cached = _PRICE_CACHE.get(sym)
    now = time.time()

    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]   # fresh WS price

    price: float | None = None

    # Try Binance REST first (fastest, no rate limit for single ticker)
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

    # Stale-cache fallback — for low-volume assets (HYPE, etc.) whose WS ticks
    # are infrequent. Use last known price if < 60s old rather than returning None.
    if price is None and cached and (now - cached[1]) < _CACHE_TTL_STALE:
        return cached[0]

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


# ---------------------------------------------------------------------------
# Momentum helpers
# ---------------------------------------------------------------------------

def _price_momentum(symbol: str, window_secs: int) -> float | None:
    """
    Returns the % price change over the last `window_secs` seconds.
    Positive = rising, negative = falling. None if insufficient history.
    """
    with _PRICE_LOCK:
        hist = list(_PRICE_HISTORY.get(symbol.upper(), []))
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
    with _PRICE_LOCK:
        hist = list(_PRICE_HISTORY.get(sym, []))
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


def _consecutive_window_trend(symbol: str, n_windows: int = 12) -> float | None:
    """
    Looks at the last n_windows completed 5-minute windows and returns
    a score from -1.0 to +1.0:
      +1.0 = all windows closed UP (strong upward trend)
      -1.0 = all windows closed DOWN (strong downward trend)
       0.0 = mixed (no trend)

    Primary source: session_log.jsonl actual_direction (ground truth for every
    window observed, not just traded windows).
    Fallback: in-memory price history ticks (limited to ~1h depth).
    """
    sym = symbol.upper()

    # ── Primary: session_log actual_direction ─────────────────────────────────
    try:
        import json as _json
        from pathlib import Path as _Path
        _log = _Path("data/session_log.jsonl")
        if _log.exists():
            directions = []
            with _log.open() as fh:
                for line in fh:
                    try:
                        e = _json.loads(line)
                        if e.get("symbol") == sym and e.get("actual_direction"):
                            directions.append(e["actual_direction"])
                    except Exception:
                        pass
            recent = directions[-n_windows:]
            if len(recent) >= 2:
                score = sum(1 if d == "UP" else -1 for d in recent) / len(recent)
                return float(score)
    except Exception:
        pass

    # ── Fallback: price history ticks ─────────────────────────────────────────
    with _PRICE_LOCK:
        hist = list(_PRICE_HISTORY.get(sym, []))
    if len(hist) < 10:
        return None
    now = time.time()
    results = []
    for i in range(1, n_windows + 1):
        end_target   = now - i * 300
        start_target = now - (i + 1) * 300
        end_tick   = min(hist, key=lambda x: abs(x[1] - end_target))
        start_tick = min(hist, key=lambda x: abs(x[1] - start_target))
        if abs(end_tick[1] - end_target) > 60 or abs(start_tick[1] - start_target) > 60:
            continue
        if start_tick[0] <= 0:
            continue
        window_ret = (end_tick[0] - start_tick[0]) / start_tick[0]
        results.append(1 if window_ret > 0 else -1)
    if len(results) < 2:
        return None
    return sum(results) / len(results)


# ---------------------------------------------------------------------------
# Oracle lag / BTC leadership
# ---------------------------------------------------------------------------

# Assets that BTC leads (moves before them in correlated markets)
_BTC_LED_ALTS = frozenset({"ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE",
                           "AVAX", "LINK", "ADA", "LTC", "DOT", "MATIC",
                           "SUI", "PEPE", "WIF", "TRX"})


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


# ---------------------------------------------------------------------------
# Multi-timeframe consensus
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Trade-count conviction filter
# ---------------------------------------------------------------------------

# Trade rate tracking — fed by BinanceWSFeed aggTrade handler
_TRADE_TIMES: dict[str, _deque] = {}   # symbol → deque of timestamps (last 10 min)
_TRADE_RATE_WINDOW = 600   # seconds of history to keep
_TRADE_RATE_AVG_WINDOW = 30  # seconds for rate calculation


def record_trade_tick(symbol: str) -> None:
    """Call from BinanceWSFeed for every aggTrade message received."""
    sym = symbol.upper()
    now = time.time()
    with _PRICE_LOCK:
        dq = _TRADE_TIMES.setdefault(sym, _deque())
        dq.append(now)
        cutoff = now - _TRADE_RATE_WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()


def _get_trade_rate(symbol: str, window_secs: int = 30) -> float:
    """Returns trades/sec in the last window_secs for symbol. 0.0 if no data."""
    sym = symbol.upper()
    with _PRICE_LOCK:
        dq = _TRADE_TIMES.get(sym)
        if not dq:
            return 0.0
        cutoff = time.time() - window_secs
        count = sum(1 for t in dq if t >= cutoff)
    return count / window_secs


def _get_avg_trade_rate(symbol: str) -> float:
    """Rolling 10-min average trade rate for baseline comparison."""
    return _get_trade_rate(symbol, window_secs=_TRADE_RATE_WINDOW)


# ---------------------------------------------------------------------------
# Binance funding rate signal
# ---------------------------------------------------------------------------

_FUNDING_CACHE: dict[str, tuple[float, float]] = {}  # symbol → (rate, ts)
_FUNDING_TTL = 60.0


def _get_funding_rate(symbol: str) -> float | None:
    """
    Returns the current perpetual futures funding rate for symbol.
    Positive = longs pay shorts (market leans bullish).
    Negative = shorts pay longs (market leans bearish).
    Cached for 60s. Returns None if unavailable.
    """
    sym = symbol.upper()
    binance_sym = sym + "USDT"
    cached = _FUNDING_CACHE.get(sym)
    now = time.time()
    if cached and (now - cached[1]) < _FUNDING_TTL:
        return cached[0]
    try:
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={binance_sym}",
            timeout=3,
        )
        r.raise_for_status()
        rate = float(r.json()["lastFundingRate"])
        _FUNDING_CACHE[sym] = (rate, now)
        return rate
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Binance open interest delta
# ---------------------------------------------------------------------------

_OI_CACHE: dict[str, list[tuple[float, float]]] = {}  # symbol → [(oi, ts), ...]
_OI_TTL = 30.0


def _get_oi_delta(symbol: str) -> float | None:
    """
    Returns % change in open interest over last 2 readings.
    Positive = OI rising (conviction), negative = OI falling (exhaustion).
    None if unavailable.
    """
    sym = symbol.upper()
    binance_sym = sym + "USDT"
    now = time.time()
    history = _OI_CACHE.setdefault(sym, [])
    if not history or (now - history[-1][1]) >= _OI_TTL:
        try:
            r = requests.get(
                f"https://fapi.binance.com/fapi/v1/openInterest?symbol={binance_sym}",
                timeout=3,
            )
            r.raise_for_status()
            oi = float(r.json()["openInterest"])
            history.append((oi, now))
            if len(history) > 20:
                history[:] = history[-20:]
        except Exception:
            return None
    if len(history) < 2:
        return None
    old_oi, new_oi = history[-2][0], history[-1][0]
    if old_oi <= 0:
        return None
    return (new_oi - old_oi) / old_oi


# ---------------------------------------------------------------------------
# Coinbase price divergence
# ---------------------------------------------------------------------------

_COINBASE_CACHE: dict[str, tuple[float, float]] = {}  # symbol → (price, ts)
_COINBASE_TTL = 10.0
_COINBASE_SYMS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}


def _get_coinbase_price(symbol: str) -> float | None:
    """Fetch spot price from Coinbase. Returns None if unavailable."""
    sym = symbol.upper()
    if sym not in _COINBASE_SYMS:
        return None
    cached = _COINBASE_CACHE.get(sym)
    now = time.time()
    if cached and (now - cached[1]) < _COINBASE_TTL:
        return cached[0]
    try:
        r = requests.get(
            f"https://api.coinbase.com/v2/prices/{sym}-USD/spot",
            timeout=3,
        )
        r.raise_for_status()
        price = float(r.json()["data"]["amount"])
        _COINBASE_CACHE[sym] = (price, now)
        return price
    except Exception:
        return None


def _cross_exchange_divergence(symbol: str) -> float:
    """
    Returns (coinbase_price - binance_price) / binance_price.
    Positive = Coinbase higher = upward arbitrage pressure on Binance.
    0.0 if data unavailable.
    """
    sym = symbol.upper()
    binance = _PRICE_CACHE.get(sym)
    if not binance or binance[0] <= 0:
        return 0.0
    coinbase = _get_coinbase_price(sym)
    if coinbase is None or coinbase <= 0:
        return 0.0
    return (coinbase - binance[0]) / binance[0]


# ---------------------------------------------------------------------------
# Fear & Greed index
# ---------------------------------------------------------------------------

_FNG_CACHE: tuple[int, str, float] | None = None  # (value, classification, ts)
_FNG_TTL = 3600.0  # 1 hour (it changes once daily)


def _get_fear_greed() -> tuple[int, str] | None:
    """
    Returns (value, classification) from alternative.me Fear & Greed Index.
    value: 0-100 (0=extreme fear, 100=extreme greed)
    classification: "Extreme Fear" | "Fear" | "Neutral" | "Greed" | "Extreme Greed"
    Cached for 1 hour. Returns None if unavailable.
    """
    global _FNG_CACHE
    now = time.time()
    if _FNG_CACHE and (now - _FNG_CACHE[2]) < _FNG_TTL:
        return _FNG_CACHE[0], _FNG_CACHE[1]
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        r.raise_for_status()
        d = r.json()["data"][0]
        value = int(d["value"])
        classification = d["value_classification"]
        _FNG_CACHE = (value, classification, now)
        return value, classification
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Market regime detection
# ---------------------------------------------------------------------------

def _market_regime(symbol: str) -> str:
    """
    Detects whether the market is trending or choppy based on recent price history.
    Returns: "TRENDING_UP" | "TRENDING_DOWN" | "CHOPPY" | "UNKNOWN"

    Logic:
    - Trending: consistent direction across 30s, 60s, 5m momentum + consecutive windows agree
    - Choppy: timeframes conflict or momentum magnitude is very low
    """
    sym = symbol.upper()
    m30 = _price_momentum(sym, 30)
    m60 = _price_momentum(sym, 60)
    m5m = _price_momentum(sym, 300)
    cw  = _consecutive_window_trend(sym)

    available = [x for x in [m30, m60, m5m] if x is not None]
    if len(available) < 2:
        return "UNKNOWN"

    # Directions
    dirs = [1 if x > 0 else -1 for x in available]
    if cw is not None:
        dirs.append(1 if cw > 0 else -1)

    n_up   = sum(1 for d in dirs if d > 0)
    n_down = sum(1 for d in dirs if d < 0)
    total  = len(dirs)

    # Strong trend: 3/4 or better agreement + meaningful magnitude
    magnitude = sum(abs(x) for x in available) / len(available)
    if n_up / total >= 0.75 and magnitude > 0.001:
        return "TRENDING_UP"
    if n_down / total >= 0.75 and magnitude > 0.001:
        return "TRENDING_DOWN"

    # Choppy: timeframes conflict
    if n_up > 0 and n_down > 0:
        return "CHOPPY"

    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Alpha decay detection
# ---------------------------------------------------------------------------

def _alpha_decay_score(symbol: str | None = None) -> float:
    """
    Reads session_log.jsonl to compute directional persistence trend.
    Returns a score from -1.0 (decaying/mean-reverting) to +1.0 (improving/trending).
    0.0 = stable or insufficient data.

    Uses actual_direction from ALL windows (not trade-filtered signal_accuracy).
    Measures whether directional momentum is strengthening or weakening by
    comparing the UP fraction of the last 10 windows vs. last 30 windows.
    """
    try:
        import json as _json
        from pathlib import Path as _Path
        _log = _Path("data/session_log.jsonl")
        if not _log.exists():
            return 0.0

        # Collect actual_direction per symbol
        by_sym: dict[str, list[str]] = {}
        with _log.open() as fh:
            for line in fh:
                try:
                    e = _json.loads(line)
                    sym = e.get("symbol")
                    d   = e.get("actual_direction")
                    if sym and d:
                        by_sym.setdefault(sym, []).append(d)
                except Exception:
                    pass

        def _trend_score(dirs: list[str]) -> float:
            """
            Compare directional consistency of last 10 vs. last 30 windows.
            Uses |net_direction| / n as the consistency measure:
              1.0 = all same direction (perfect trend)
              0.0 = perfectly mixed
            """
            if len(dirs) < 10:
                return 0.0
            def _consistency(window):
                if not window:
                    return 0.0
                ups = sum(1 for d in window if d == "UP")
                return abs(ups / len(window) - 0.5) * 2  # 0=random, 1=all same direction
            recent_10 = _consistency(dirs[-10:])
            recent_30 = _consistency(dirs[-30:]) if len(dirs) >= 30 else recent_10
            return recent_10 - recent_30  # positive = more consistent recently

        if symbol:
            dirs = by_sym.get(symbol.upper(), [])
            return _trend_score(dirs)
        else:
            scores = [_trend_score(d) for d in by_sym.values() if len(d) >= 10]
            return float(sum(scores) / len(scores)) if scores else 0.0

    except Exception:
        return 0.0
