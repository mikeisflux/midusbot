"""
Trading strategies for Polymarket.

Strategy 1 — Momentum + Order-Book Imbalance
─────────────────────────────────────────────
Combines 24-h price momentum with order-book bid/ask imbalance.
Signal weights, thresholds, and adjustments are all dynamic — the
AdaptiveLearner can tune them online based on trade outcomes.

Strategy 2 — Latency Arbitrage (BTC / crypto contracts)
───────────────────────────────────────────────────────
Polymarket updates crypto-outcome contract prices slower than real-time
exchange feeds.  This strategy:
  1. Pulls the live BTC (or other asset) price from external feeds.
  2. Compares it to the implied price in the Polymarket contract.
  3. When the lag exceeds a configurable threshold it places a rapid
     limit order to capture the mispricing before the market catches up.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import requests
from loguru import logger

from src.client import Market, OrderBook, PricePoint
import config

if TYPE_CHECKING:
    from src.learner import StrategyParams

# ---------------------------------------------------------------------------
# Defaults (used when no adaptive params are provided)
# ---------------------------------------------------------------------------

DEFAULT_SIGNAL_THRESHOLD = 0.15
DEFAULT_MAX_SIGNAL_ADJUST = 0.07
DEFAULT_MOMENTUM_WEIGHT = 0.50
DEFAULT_IMBALANCE_WEIGHT = 0.50
OB_LEVELS = 10
MOMENTUM_NORMALISER = 0.20


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class TradeSignal:
    market_id: str
    question: str
    side: str            # "YES" | "NO"
    token_id: str
    market_price: float  # current market mid price for the chosen token
    fair_value: float    # our estimated fair price
    edge: float          # fair_value - market_price (positive = we have edge)
    signal: float        # composite signal [-1, 1]
    confidence: str      # "LOW" | "MEDIUM" | "HIGH"
    # Sub-signals (used by learner to attribute wins/losses)
    momentum_signal: float  = 0.0
    imbalance_signal: float = 0.0
    # Latency arb fields
    is_latency_arb: bool = False
    lag_pct: float = 0.0
    # Time urgency (hours until market closes; None = unknown)
    hours_to_close: float | None = None
    # Best ask at signal time — used for aggressive fills on ultra-short markets
    best_ask: float | None = None

    def __str__(self) -> str:
        tag = "[LATENCY-ARB] " if self.is_latency_arb else ""
        return (
            f"{tag}[{self.confidence}] {self.side} {self.question[:60]} | "
            f"mkt={self.market_price:.3f}  fv={self.fair_value:.3f}  "
            f"edge={self.edge:+.3f}  sig={self.signal:+.3f}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 1: Momentum + Imbalance
# ═══════════════════════════════════════════════════════════════════════════

class MomentumImbalanceStrategy:
    """
    Scans a single market and returns a TradeSignal if an opportunity is
    found.  All tuneable constants come from `params` (set by the learner).
    """

    def __init__(self, params: StrategyParams | None = None) -> None:
        self._params = params

    # -- properties for current param values (fallback to defaults) --------

    @property
    def _momentum_weight(self) -> float:
        return self._params.momentum_weight if self._params else DEFAULT_MOMENTUM_WEIGHT

    @property
    def _imbalance_weight(self) -> float:
        return self._params.imbalance_weight if self._params else DEFAULT_IMBALANCE_WEIGHT

    @property
    def _signal_threshold(self) -> float:
        return self._params.signal_threshold if self._params else DEFAULT_SIGNAL_THRESHOLD

    @property
    def _max_signal_adjust(self) -> float:
        return self._params.max_signal_adjust if self._params else DEFAULT_MAX_SIGNAL_ADJUST

    # -- analysis ----------------------------------------------------------

    def analyse(
        self,
        market: Market,
        order_book: OrderBook | None,
        price_history: list[PricePoint],
    ) -> TradeSignal | None:
        try:
            mom_sig = self._momentum_signal(market, price_history)
            imb_sig = self._imbalance_signal(order_book)

            composite = (
                self._momentum_weight * mom_sig
                + self._imbalance_weight * imb_sig
            )

            logger.debug(
                f"{market.question[:50]} | mom={mom_sig:+.3f}  "
                f"imb={imb_sig:+.3f}  comp={composite:+.3f}"
            )

            if abs(composite) < self._signal_threshold:
                return None

            # Choose direction
            if composite > 0:
                side = "YES"
                token = market.yes_token
                mid = order_book.mid if order_book else market.yes_price
            else:
                side = "NO"
                token = market.no_token
                mid = (1.0 - order_book.mid) if order_book else market.no_price
                composite = -composite  # flip for edge calc

            fair_value = mid * (1.0 + composite * self._max_signal_adjust)
            fair_value = float(np.clip(fair_value, 0.01, 0.99))
            edge = fair_value - mid

            if edge < config.MIN_EDGE:
                return None

            confidence = (
                "HIGH"   if composite > 0.5 else
                "MEDIUM" if composite > 0.3 else
                "LOW"
            )

            return TradeSignal(
                market_id=market.id,
                question=market.question,
                side=side,
                token_id=token.token_id,
                market_price=mid,
                fair_value=fair_value,
                edge=edge,
                signal=composite,
                confidence=confidence,
                momentum_signal=mom_sig,
                imbalance_signal=imb_sig,
            )

        except Exception as exc:
            logger.warning(f"Strategy error on market {market.id}: {exc}")
            return None

    # -- sub-signals -------------------------------------------------------

    def _momentum_signal(
        self, market: Market, price_history: list[PricePoint]
    ) -> float:
        if len(price_history) < 2:
            return 0.0
        recent = price_history[-1].price
        lookback = min(24, len(price_history) - 1)
        past = price_history[-1 - lookback].price
        if past <= 0:
            return 0.0
        change = (recent - past) / past
        return float(np.clip(change / MOMENTUM_NORMALISER, -1.0, 1.0))

    def _imbalance_signal(self, ob: OrderBook | None) -> float:
        if ob is None:
            return 0.0
        bid_qty = sum(float(b.get("size", 0)) for b in ob.bids[:OB_LEVELS])
        ask_qty = sum(float(a.get("size", 0)) for a in ob.asks[:OB_LEVELS])
        total = bid_qty + ask_qty
        if total == 0:
            return 0.0
        return float((bid_qty - ask_qty) / total)


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 2: Latency Arbitrage
# ═══════════════════════════════════════════════════════════════════════════

# ── Price feeds ──────────────────────────────────────────────────────────────
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

_PRICE_CACHE: dict[str, tuple[float, float]] = {}   # symbol → (price, timestamp)
_CACHE_TTL = 2.0   # seconds

# Rolling 60-second price history for momentum: symbol → [(price, ts), ...]
_PRICE_HISTORY: dict[str, list[tuple[float, float]]] = {}
_HISTORY_WINDOW = 90  # keep 90 seconds of ticks


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


def _price_momentum_60s(symbol: str) -> float | None:
    """
    Returns the % price change over the last ~60 seconds.
    Positive = price rising, negative = price falling.
    Returns None if insufficient history.
    """
    hist = _PRICE_HISTORY.get(symbol.upper(), [])
    now = time.time()
    # Find oldest tick within 60-90 s
    window = [(p, t) for p, t in hist if now - t <= 75]
    if len(window) < 2:
        return None
    oldest_price = window[0][0]
    newest_price = window[-1][0]
    if oldest_price <= 0:
        return None
    return (newest_price - oldest_price) / oldest_price


# Keyword patterns that indicate BTC price-level contracts
_BTC_KEYWORDS = [
    "bitcoin", "btc", "btc/usd", "bitcoin price",
]


def _extract_btc_target(question: str) -> float | None:
    """
    Try to extract the dollar target from a market question like:
      "Will Bitcoin be above $70,000 on June 30?"
      "Will Bitcoin hit $100k by year end?"
      "Will Bitcoin reach $1m before GTA VI?"
    Returns the dollar figure or None.
    """
    q = question.lower()
    if not any(kw in q for kw in _BTC_KEYWORDS):
        return None

    import re
    # Match $70,000 / $70k / $1m / $1.5b with optional suffix k/m/b
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*([kmb])\b", q)
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            suffix = m.group(2)
            if suffix == "k":
                val *= 1_000
            elif suffix == "m":
                val *= 1_000_000
            elif suffix == "b":
                val *= 1_000_000_000
            return val
        except ValueError:
            pass

    # Fall back: plain number like $70,000 or $70000
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", q)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
        # Numbers under 500 are probably written as "70" meaning 70k
        if val < 500:
            val *= 1000
        return val
    except ValueError:
        return None


class LatencyArbStrategy:
    """
    Detects when Polymarket crypto contracts lag behind real-time price feeds
    and generates a signal to exploit the mispricing.

    Parameters:
        min_lag_pct – minimum % divergence to act on (default 0.3 %)
    """

    def __init__(self, min_lag_pct: float = 0.003) -> None:
        self.min_lag_pct = min_lag_pct

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        target = _extract_btc_target(market.question)
        if target is None:
            return None  # not a BTC price market

        live_price = _fetch_btc_price()
        if live_price is None:
            return None

        # Safety: skip markets where the live price is more than 30% away from
        # the target — those are long-dated prediction markets (e.g. "Will BTC
        # hit $1M?"), not genuine latency-arb opportunities.
        distance_pct = abs(live_price - target) / target
        if distance_pct > 0.30:
            logger.debug(
                f"[LATENCY-ARB] Skipping {market.question[:50]} — "
                f"BTC ${live_price:,.0f} is {distance_pct:.0%} away from "
                f"target ${target:,.0f} (threshold 30%)"
            )
            return None

        # Implied probability: how likely is BTC to be above $target?
        # Simple model: if live BTC is far above target → probability ≈ 1,
        # if far below → ≈ 0, linear in the band ±10 % around target.
        band = target * 0.10
        implied_prob = float(np.clip((live_price - target + band) / (2 * band), 0.01, 0.99))

        # Market probability
        mkt_yes = order_book.mid if order_book else market.yes_price

        lag = implied_prob - mkt_yes          # positive → market is too low → buy YES
        lag_pct = abs(lag)

        if lag_pct < self.min_lag_pct:
            return None

        if lag > 0:
            side = "YES"
            token = market.yes_token
            mkt_price = mkt_yes
        else:
            side = "NO"
            token = market.no_token
            mkt_price = 1.0 - mkt_yes

        fair_value = float(np.clip(implied_prob if side == "YES" else 1.0 - implied_prob, 0.01, 0.99))
        edge = fair_value - mkt_price

        if edge < 0.002:  # ultra-thin arb still needs some edge
            return None

        confidence = (
            "HIGH"   if lag_pct > 0.01 else
            "MEDIUM" if lag_pct > 0.005 else
            "LOW"
        )

        logger.info(
            f"[LATENCY-ARB] BTC live=${live_price:,.0f}  target=${target:,.0f}  "
            f"implied={implied_prob:.3f}  mkt={mkt_yes:.3f}  lag={lag:+.4f} "
            f"→ {side}"
        )

        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_price,
            fair_value=fair_value,
            edge=edge,
            signal=lag_pct,
            confidence=confidence,
            momentum_signal=0.0,
            imbalance_signal=0.0,
            is_latency_arb=True,
            lag_pct=lag_pct,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 3: Up/Down 5-Minute Momentum
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


def _detect_updown_market(question: str) -> str | None:
    """
    Returns the asset symbol if the question is an Up/Down short-interval
    market (e.g. "XRP Up or Down - March 3, 12:00PM-12:05PM ET").
    Returns None otherwise.
    """
    q = question.lower()
    if "up or down" not in q and "up/down" not in q:
        return None
    for kw, sym in _UPDOWN_ASSETS.items():
        if kw in q:
            return sym
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

        # Warm up the price history on every call (lightweight, cached)
        live_price = _fetch_price(symbol)
        if live_price is None:
            return None

        mom = _price_momentum_60s(symbol)
        if mom is None or abs(mom) < self.min_momentum_pct:
            return None   # not enough momentum data yet

        # Momentum → fair value
        # Scale: 0.05% → ~55%; 0.2% → ~66%; 0.5% → ~80%; 1%+ → ~92%
        # The stronger the 60s move the more certain the 5-min direction
        raw_confidence = 0.50 + min(abs(mom) / 0.010, 1.0) * 0.42
        fair_prob = float(np.clip(raw_confidence, 0.51, 0.92))

        # Determine which outcome to bet
        if mom > 0:
            # Price rising → bet UP (YES)
            side = "YES"
            token = market.yes_token
            mkt_price = order_book.mid if order_book else market.yes_price
            fair_value = fair_prob
        else:
            # Price falling → bet DOWN (NO)
            side = "NO"
            token = market.no_token
            mkt_price = (1.0 - order_book.mid) if order_book else market.no_price
            fair_value = fair_prob

        fair_value = float(np.clip(fair_value, 0.01, 0.99))
        edge = fair_value - mkt_price

        if edge < config.MIN_EDGE:
            return None

        # HIGH confidence = strong momentum → Kelly sizes up aggressively
        # mirrors the 200-share trades the reference trader places on strong moves
        confidence = (
            "HIGH"   if abs(mom) > 0.005 else   # >0.5%/60s — very strong
            "MEDIUM" if abs(mom) > 0.002 else   # >0.2%/60s — clear trend
            "LOW"                                # weak but above threshold
        )

        direction = "UP" if mom > 0 else "DOWN"
        logger.info(
            f"[UPDOWN] {symbol} {direction} {mom:+.4%}/60s  "
            f"fair={fair_value:.3f}  mkt={mkt_price:.3f}  edge={edge:+.3f}  "
            f"→ {side} [{confidence}]  \"{market.question[:50]}\""
        )

        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_price,
            fair_value=fair_value,
            edge=edge,
            signal=float(np.clip(abs(mom) * 200, 0.0, 1.0)),  # normalised [0-1] for Kelly
            confidence=confidence,
            momentum_signal=mom,
            imbalance_signal=0.0,
            is_latency_arb=False,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 4: BTC Price Level Markets (daily above/below + monthly reach/dip)
# ═══════════════════════════════════════════════════════════════════════════
#
# Modelled on Attentive-Silica ($723K PNL in 1 month, 67% win rate):
#
#  Pattern A — Daily "above $X" safe yield
#    BTC safely above threshold → buy YES at 90-97¢, collect 3-7% edge.
#
#  Pattern B — Monthly/weekly "reach $X" cheap options
#    Buy YES at 0.4-2¢ when true touch probability is 5-30%.
#    Market massively underprices BTC volatility on far-out levels.
#    This is how the +$195K trade (+7,360%) happened.
#
# ═══════════════════════════════════════════════════════════════════════════

import math as _math


def _norm_cdf(z: float) -> float:
    """Fast normal CDF (Abramowitz & Stegun)."""
    if z >= 8.0:  return 1.0
    if z <= -8.0: return 0.0
    t = 1.0 / (1.0 + 0.2316419 * abs(z))
    d = 0.3989423 * _math.exp(-z * z / 2.0)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.7814779 + t * (-1.8212560 + t * 1.3302744))))
    return (1.0 - p) if z >= 0 else p


def _prob_above(current: float, threshold: float, days: float, daily_vol: float = 0.04) -> float:
    """P(log-normal price ends above threshold after `days` days)."""
    if days <= 0:
        return 1.0 if current >= threshold else 0.0
    vol = daily_vol * _math.sqrt(days)
    z   = _math.log(current / threshold) / vol
    return _norm_cdf(z)


def _prob_touch(current: float, target: float, days: float, daily_vol: float = 0.04) -> float:
    """
    P(price touches `target` at any point over `days` days).
    Reflection principle: P(touch) = 2 * N(-|log(current/target)| / vol).
    """
    if days <= 0:
        return 0.0
    vol      = daily_vol * _math.sqrt(days)
    log_dist = abs(_math.log(current / target))
    return min(2.0 * _norm_cdf(-log_dist / vol), 0.999)


def _parse_btc_level(question: str):
    """
    Parse a BTC price-level question.
    Returns (mtype, p1, p2) or None.
    mtype: 'above' | 'below' | 'between' | 'reach' | 'dip'
    """
    q = question.lower()
    if "bitcoin" not in q and "btc" not in q:
        return None

    def _px(s: str) -> float | None:
        s = s.replace(",", "").strip()
        m = _re.match(r"(\d+(?:\.\d+)?)([kmb]?)", s)
        if not m:
            return None
        v   = float(m.group(1))
        mul = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(m.group(2), 1)
        return v * mul

    for pat, mtype in (
        (r"(?:be\s+)?(?:above|greater\s+than)\s+\$?([\d,]+(?:\.\d+)?)", "above"),
        (r"(?:be\s+)?(?:less\s+than|below)\s+\$?([\d,]+(?:\.\d+)?)",    "below"),
        (r"reach\s+\$?([\d,]+(?:\.\d+)?)",                               "reach"),
        (r"dip\s+to\s+\$?([\d,]+(?:\.\d+)?)",                            "dip"),
    ):
        m = _re.search(pat, q)
        if m:
            p = _px(m.group(1))
            return (mtype, p, None) if p else None

    m = _re.search(r"between\s+\$?([\d,]+)\s+and\s+\$?([\d,]+)", q)
    if m:
        p1, p2 = _px(m.group(1)), _px(m.group(2))
        return ("between", p1, p2) if p1 and p2 else None

    return None


class BTCLevelStrategy:
    """
    Trades BTC daily/weekly/monthly price level markets.

    Four sub-patterns run simultaneously:

    A) Daily above/below  — safe yield when BTC is far from threshold.
    B) Weekly above/below — moderate certainty, multi-day window.
    C) Reach/dip cheap options — buy YES at <5¢ when fair prob is 3×+
       the market price. The +$195K trade was this pattern.
    D) Range (between) — buy NO when BTC is far outside the range.
    """

    _DAILY_VOL             = 0.04   # 4% daily BTC vol (conservative)
    _MIN_CHEAP_RATIO       = 3.0    # fair / market must be >= 3× for cheap options
    _CHEAP_OPTION_MAX_PRICE = 0.05  # only pattern C when market price <= 5¢

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        parsed = _parse_btc_level(market.question)
        if parsed is None:
            return None

        mtype, p1, p2 = parsed
        current_btc = _fetch_price("BTC")
        if not current_btc or not p1:
            return None

        # Days to resolution
        days = 1.0
        if market.end_date:
            try:
                from datetime import datetime, timezone as _tz
                end  = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                days = max(0.05, (end - datetime.now(_tz.utc)).total_seconds() / 86400)
            except Exception:
                pass

        mkt_yes = order_book.mid if order_book else market.yes_price
        mkt_no  = 1.0 - mkt_yes

        # ── Fair probabilities ────────────────────────────────────────────
        if mtype == "above":
            fair_yes = _prob_above(current_btc, p1, days, self._DAILY_VOL)
        elif mtype == "below":
            fair_yes = 1.0 - _prob_above(current_btc, p1, days, self._DAILY_VOL)
        elif mtype in ("reach", "dip"):
            fair_yes = _prob_touch(current_btc, p1, days, self._DAILY_VOL)
        elif mtype == "between":
            fair_yes = (_prob_above(current_btc, p1, days, self._DAILY_VOL)
                        - _prob_above(current_btc, p2, days, self._DAILY_VOL))
        else:
            return None

        fair_yes = float(np.clip(fair_yes, 0.001, 0.999))
        fair_no  = 1.0 - fair_yes
        edge_yes = fair_yes - mkt_yes
        edge_no  = fair_no  - mkt_no

        # ── Pattern C: cheap touch options ────────────────────────────────
        if mtype in ("reach", "dip") and mkt_yes <= self._CHEAP_OPTION_MAX_PRICE:
            ratio = fair_yes / max(mkt_yes, 0.001)
            if ratio >= self._MIN_CHEAP_RATIO:
                confidence = "HIGH" if ratio >= 8 else "MEDIUM" if ratio >= 5 else "LOW"
                logger.info(
                    f"[BTC-LEVEL CHEAP] {mtype.upper()} ${p1:,.0f}  BTC=${current_btc:,.0f}  "
                    f"fair={fair_yes:.3f}  mkt={mkt_yes:.4f}  ratio={ratio:.1f}×  days={days:.0f}  "
                    f"→ YES [{confidence}]  \"{market.question[:50]}\""
                )
                return TradeSignal(
                    market_id=market.id,
                    question=market.question,
                    side="YES",
                    token_id=market.yes_token.token_id,
                    market_price=mkt_yes,
                    fair_value=fair_yes,
                    edge=fair_yes - mkt_yes,
                    signal=fair_yes - mkt_yes,
                    confidence=confidence,
                    is_latency_arb=False,
                )

        # ── Patterns A/B/D: standard edge threshold ───────────────────────
        if edge_yes >= edge_no and edge_yes > config.MIN_EDGE:
            side, token, mkt_p, fair_p, edge = "YES", market.yes_token, mkt_yes, fair_yes, edge_yes
        elif edge_no > config.MIN_EDGE:
            side, token, mkt_p, fair_p, edge = "NO",  market.no_token,  mkt_no,  fair_no,  edge_no
        else:
            return None

        confidence = "HIGH" if edge > 0.15 else "MEDIUM" if edge > 0.08 else "LOW"
        logger.info(
            f"[BTC-LEVEL] {mtype.upper()} ${p1:,.0f}  BTC=${current_btc:,.0f}  "
            f"fair={fair_p:.3f}  mkt={mkt_p:.3f}  edge={edge:+.3f}  days={days:.1f}  "
            f"→ {side} [{confidence}]  \"{market.question[:50]}\""
        )
        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_p,
            fair_value=fair_p,
            edge=edge,
            signal=edge,
            confidence=confidence,
            is_latency_arb=False,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 5: Live Sports Settlement Lag  (kch123 pattern)
# ═══════════════════════════════════════════════════════════════════════════
#
# kch123 turned $340 → $10M+ by exploiting Polymarket's live sports markets.
# His Super Bowl edge: $1.8M in one day on Seahawks spreads.
#
# The pattern: Polymarket's oracle updates with a delay during live games.
# If a team scores making the outcome ~95% certain, the market still shows
# 50-60¢ for 30-120 seconds. That's a massive edge.
#
# This strategy uses ESPN's free public API for live scores + win probability,
# then finds the matching Polymarket market and trades the discrepancy.
#
# No API key needed — ESPN data is publicly accessible.
# ═══════════════════════════════════════════════════════════════════════════

_SPORTS_PRICE_CACHE: dict[str, tuple[dict, float]] = {}
_SPORTS_CACHE_TTL = 15.0   # refresh live scores every 15s


def _fetch_live_sports() -> list[dict]:
    """
    Fetch live game scores + win probabilities from ESPN's public API.
    Returns list of {home, away, home_score, away_score, home_win_prob,
    status, sport, league}.
    """
    endpoints = [
        ("nfl",  "americanfootball_nfl",  "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"),
        ("nba",  "basketball_nba",        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"),
        ("mlb",  "baseball_mlb",          "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"),
        ("nhl",  "hockey_nhl",            "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"),
        ("soccer_epl", "soccer",          "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
    ]
    games = []
    now = time.time()

    for sport, league, url in endpoints:
        cached = _SPORTS_PRICE_CACHE.get(sport)
        if cached and (now - cached[1]) < _SPORTS_CACHE_TTL:
            games.extend(cached[0].get("games", []))
            continue
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                continue
            data = resp.json()
            sport_games = []
            for event in data.get("events", []):
                comp = event.get("competitions", [{}])[0]
                status = comp.get("status", {}).get("type", {})
                if not status.get("inProgress", False):
                    continue   # only care about LIVE games
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue
                home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

                # Win probability from ESPN (available for NFL/NBA/MLB)
                home_win_prob = 0.5
                for pred in comp.get("predictor", {}).get("homeTeam", {}).get("statistics", []):
                    if pred.get("name") == "winPercentage":
                        home_win_prob = float(pred.get("displayValue", "50").replace("%", "")) / 100
                        break

                # Fallback: infer from score + time remaining
                clock = status.get("detail", "")
                home_score = int(home.get("score", 0) or 0)
                away_score = int(away.get("score", 0) or 0)

                g = {
                    "sport":         sport,
                    "home":          home.get("team", {}).get("displayName", ""),
                    "away":          away.get("team", {}).get("displayName", ""),
                    "home_abbr":     home.get("team", {}).get("abbreviation", ""),
                    "away_abbr":     away.get("team", {}).get("abbreviation", ""),
                    "home_score":    home_score,
                    "away_score":    away_score,
                    "home_win_prob": home_win_prob,
                    "clock":         clock,
                    "period":        status.get("period", 0),
                }
                sport_games.append(g)
                games.append(g)
            _SPORTS_PRICE_CACHE[sport] = ({"games": sport_games}, now)
        except Exception:
            pass
    return games


def _match_sports_market(market_question: str, live_games: list[dict]) -> dict | None:
    """
    Fuzzy-match a Polymarket market question to a live game.
    Returns the live game dict if matched, else None.
    """
    q = market_question.lower()
    for game in live_games:
        home = game["home"].lower()
        away = game["away"].lower()
        home_abbr = game["home_abbr"].lower()
        away_abbr = game["away_abbr"].lower()
        # Match if both team names (or abbreviations) appear in the question
        home_match = any(part in q for part in [home, home_abbr, home.split()[-1]])
        away_match = any(part in q for part in [away, away_abbr, away.split()[-1]])
        if home_match and away_match:
            return game
    return None


class SportsLiveStrategy:
    """
    Exploits Polymarket's live sports market settlement lag.

    The kch123 pattern: during a live game, Polymarket's oracle updates
    slowly. When a team is effectively certain to win but the market
    still shows 50-60¢, that's a 30-40¢ free edge.

    Uses ESPN's public API for real-time win probability.
    No API key required.

    Edge threshold: only fires when ESPN win_prob vs market_price
    diverges by more than MIN_SPORTS_EDGE (default 15%).
    """

    MIN_SPORTS_EDGE = 0.15   # minimum edge to trade

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        live_games = _fetch_live_sports()
        if not live_games:
            return None

        game = _match_sports_market(market.question, live_games)
        if game is None:
            return None

        home_win_prob = game["home_win_prob"]
        away_win_prob = 1.0 - home_win_prob

        mkt_yes = order_book.mid if order_book else market.yes_price

        # Determine which team the YES outcome represents
        q = market.question.lower()
        home_name = game["home"].lower().split()[-1]
        home_is_yes = home_name in q

        if home_is_yes:
            fair_yes = home_win_prob
        else:
            fair_yes = away_win_prob

        fair_no  = 1.0 - fair_yes
        mkt_no   = 1.0 - mkt_yes
        edge_yes = fair_yes - mkt_yes
        edge_no  = fair_no  - mkt_no

        if edge_yes >= edge_no and edge_yes >= self.MIN_SPORTS_EDGE:
            side, token, mkt_p, fair_p, edge = "YES", market.yes_token, mkt_yes, fair_yes, edge_yes
        elif edge_no >= self.MIN_SPORTS_EDGE:
            side, token, mkt_p, fair_p, edge = "NO",  market.no_token,  mkt_no,  fair_no,  edge_no
        else:
            return None

        confidence = "HIGH" if edge > 0.30 else "MEDIUM" if edge > 0.20 else "LOW"
        logger.info(
            f"[SPORTS-LIVE] {game['away_abbr']} {game['away_score']} @ "
            f"{game['home_abbr']} {game['home_score']}  {game['clock']}  "
            f"ESPN_win={fair_yes:.2f}  mkt={mkt_p:.2f}  edge={edge:+.2f}  "
            f"→ {side} [{confidence}]  \"{market.question[:50]}\""
        )
        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_p,
            fair_value=fair_p,
            edge=edge,
            signal=edge,
            confidence=confidence,
            is_latency_arb=True,   # this IS a latency arb — oracle vs ESPN
        )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 6: Sudden Price Move Detection (news/event arb)
# ═══════════════════════════════════════════════════════════════════════════
#
# When news breaks, Polymarket prices move fast but not instantly.
# If a market's YES price jumped >15% since last loop, someone knows something.
# We follow the move assuming it continues for at least 1-2 more loops.
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Strategy 7: Sports Spread Arbitrage vs. Vegas Consensus  (kch123 pattern)
# ═══════════════════════════════════════════════════════════════════════════
#
# kch123's actual edge: Polymarket sports spread odds are frequently mispriced
# vs. the consensus line from traditional sportsbooks (DraftKings, FanDuel…).
#
# He places 14,303 directional YES buys — zero sells — and holds to resolution.
# $11.15M profit, 61% win rate, ~$1.8M on Super Bowl Seahawks spreads alone.
#
# This strategy:
#   1. Polls The Odds API (free tier: 500 req/month) for live bookmaker lines
#   2. Converts bookmaker odds → consensus probability
#   3. Fuzzy-matches Polymarket market question → game
#   4. Fires when Polymarket's implied prob diverges from Vegas by ≥ MIN_EDGE
#
# Set ODDS_API_KEY in .env to enable.  Without it, strategy silently skips.
# Free key at: https://the-odds-api.com  (takes 30 seconds to register)
# ═══════════════════════════════════════════════════════════════════════════

_ODDS_CACHE: dict[str, tuple[list, float]] = {}   # sport_key → (games, fetched_at)
_ODDS_CACHE_TTL = 60.0   # seconds — conservative to preserve free-tier quota

_ODDS_SPORTS = [
    ("americanfootball_nfl",          "nfl"),
    ("basketball_nba",                "nba"),
    ("baseball_mlb",                  "mlb"),
    ("icehockey_nhl",                 "nhl"),
    ("soccer_epl",                    "epl"),
    ("soccer_uefa_champs_league",     "ucl"),
    ("soccer_usa_mls",                "mls"),
]


def _fetch_vegas_odds() -> list[dict]:
    """
    Fetch upcoming/live odds from The Odds API.
    Returns list of normalised game dicts:
      {sport, home_team, away_team, home_prob, away_prob, commence_time}
    """
    api_key = config.ODDS_API_KEY
    if not api_key:
        return []

    all_games: list[dict] = []
    now = time.time()

    for sport_key, label in _ODDS_SPORTS:
        cached = _ODDS_CACHE.get(sport_key)
        if cached and (now - cached[1]) < _ODDS_CACHE_TTL:
            all_games.extend(cached[0])
            continue

        try:
            resp = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                params={
                    "apiKey":      api_key,
                    "regions":     "us",
                    "markets":     "h2h",
                    "oddsFormat":  "decimal",
                    "dateFormat":  "iso",
                },
                timeout=8,
            )
            if resp.status_code == 401:
                logger.warning("[ODDS-API] Invalid API key — sports spread arb disabled")
                _ODDS_CACHE[sport_key] = ([], now)
                continue
            if resp.status_code == 422:
                # Sport not currently in season
                _ODDS_CACHE[sport_key] = ([], now)
                continue
            if resp.status_code != 200:
                continue
            events = resp.json()
            sport_games: list[dict] = []
            for ev in events:
                home = ev.get("home_team", "")
                away = ev.get("away_team", "")
                bookmakers = ev.get("bookmakers", [])
                if not bookmakers:
                    continue

                # Collect h2h prices from all bookmakers, average them
                home_probs, away_probs = [], []
                for bm in bookmakers:
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        for outcome in mkt.get("outcomes", []):
                            price = float(outcome.get("price", 0))
                            if price <= 1:
                                continue
                            prob = 1.0 / price
                            if outcome.get("name") == home:
                                home_probs.append(prob)
                            elif outcome.get("name") == away:
                                away_probs.append(prob)

                if not home_probs or not away_probs:
                    continue

                # Consensus probability (average, then normalise to sum=1)
                raw_home = sum(home_probs) / len(home_probs)
                raw_away = sum(away_probs) / len(away_probs)
                total    = raw_home + raw_away
                home_prob = raw_home / total
                away_prob = raw_away / total

                g = {
                    "sport":          label,
                    "home_team":      home,
                    "away_team":      away,
                    "home_prob":      home_prob,
                    "away_prob":      away_prob,
                    "commence_time":  ev.get("commence_time", ""),
                }
                sport_games.append(g)
                all_games.append(g)

            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.debug(f"[ODDS-API] {label}: {len(sport_games)} games  quota_remaining={remaining}")
            _ODDS_CACHE[sport_key] = (sport_games, now)

        except Exception as exc:
            logger.debug(f"[ODDS-API] {sport_key} fetch failed: {exc}")

    return all_games


def _match_vegas_game(market_question: str, games: list[dict]) -> dict | None:
    """
    Fuzzy-match a Polymarket question to a Vegas game.
    Returns the game dict if a confident match is found.
    """
    q = market_question.lower()
    best: dict | None = None
    best_score = 0

    for game in games:
        home_parts = game["home_team"].lower().split()
        away_parts = game["away_team"].lower().split()

        # Score = number of distinct team name words found in the question
        score = 0
        for word in home_parts + away_parts:
            if len(word) >= 4 and word in q:
                score += 1

        # Require at least one word from each team
        home_hit = any(len(w) >= 4 and w in q for w in home_parts)
        away_hit = any(len(w) >= 4 and w in q for w in away_parts)
        if home_hit and away_hit and score > best_score:
            best_score = score
            best = game

    return best


class SportsSpreadArbStrategy:
    """
    Compares Polymarket sports market implied probabilities against the
    Vegas consensus line from The Odds API.

    When Polymarket prices a team at 0.45 but Vegas consensus says 0.62,
    the expected value of buying YES is enormous — this is the kch123 edge.

    Requires ODDS_API_KEY in .env (free tier: 500 req/month).
    Without a key this strategy is silently skipped.

    MIN_EDGE: minimum divergence to trade (default 5¢ / 5%)
    """

    MIN_EDGE = 0.05   # tighter than other strategies — Vegas lines are very efficient

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        games = _fetch_vegas_odds()
        if not games:
            return None

        game = _match_vegas_game(market.question, games)
        if game is None:
            return None

        mkt_yes = order_book.mid if order_book else market.yes_price
        mkt_no  = 1.0 - mkt_yes

        # Determine which team the YES outcome maps to
        q = market.question.lower()
        home_parts = game["home_team"].lower().split()
        away_parts = game["away_team"].lower().split()

        home_in_q = any(len(w) >= 4 and w in q for w in home_parts)
        away_in_q = any(len(w) >= 4 and w in q for w in away_parts)

        # Prefer the team whose name appears nearest "win" in the question
        # Fallback: home team = YES if home is mentioned first
        home_pos = min((q.find(w) for w in home_parts if len(w) >= 4 and w in q), default=9999)
        away_pos = min((q.find(w) for w in away_parts if len(w) >= 4 and w in q), default=9999)

        if home_in_q and (not away_in_q or home_pos <= away_pos):
            fair_yes = game["home_prob"]
            team_label = game["home_team"]
        elif away_in_q:
            fair_yes = game["away_prob"]
            team_label = game["away_team"]
        else:
            return None

        fair_no  = 1.0 - fair_yes
        edge_yes = fair_yes - mkt_yes
        edge_no  = fair_no  - mkt_no

        if edge_yes >= edge_no and edge_yes >= self.MIN_EDGE:
            side, token, mkt_p, fair_p, edge = "YES", market.yes_token, mkt_yes, fair_yes, edge_yes
        elif edge_no >= self.MIN_EDGE:
            side, token, mkt_p, fair_p, edge = "NO",  market.no_token,  mkt_no,  fair_no,  edge_no
        else:
            return None

        confidence = "HIGH" if edge > 0.15 else "MEDIUM" if edge > 0.08 else "LOW"
        logger.info(
            f"[SPORTS-SPREAD-ARB] {game['away_team']} @ {game['home_team']}  "
            f"Vegas={fair_yes:.3f}  Poly={mkt_yes:.3f}  edge={edge:+.3f}  "
            f"→ {side} on {team_label} [{confidence}]  \"{market.question[:50]}\""
        )
        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_p,
            fair_value=fair_p,
            edge=edge,
            signal=edge,
            confidence=confidence,
            is_latency_arb=True,   # price-source arb
        )


_PREV_PRICES: dict[str, float] = {}   # market_id → last YES price


def update_price_snapshot(market_id: str, yes_price: float) -> None:
    _PREV_PRICES[market_id] = yes_price


class NewsEventStrategy:
    """
    Momentum follow on sudden market price moves.

    If a market's YES price moved ≥ JUMP_THRESHOLD since last scan,
    follow the direction — the move likely reflects breaking news that
    hasn't fully propagated through the market yet.

    This is the "Trump tariff announcement" pattern: market goes from
    40¢ to 75¢ in one loop. We catch the remaining 25¢ move.
    """

    JUMP_THRESHOLD = 0.12   # 12% sudden move triggers the signal
    MIN_EDGE       = 0.08   # minimum remaining edge after the jump

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        prev = _PREV_PRICES.get(market.id)
        current = order_book.mid if order_book else market.yes_price
        update_price_snapshot(market.id, current)

        if prev is None:
            return None   # first time seeing this market

        move = current - prev
        if abs(move) < self.JUMP_THRESHOLD:
            return None

        # Follow the move direction
        if move > 0:
            # YES jumped — buy YES (momentum continuation)
            fair_yes = min(current + move * 0.5, 0.97)   # extrapolate 50% more
            edge = fair_yes - current
            if edge < self.MIN_EDGE:
                return None
            side, token, mkt_p, fair_p = "YES", market.yes_token, current, fair_yes
        else:
            # YES dumped — buy NO
            fair_no  = min((1 - current) + abs(move) * 0.5, 0.97)
            mkt_no   = 1.0 - current
            edge     = fair_no - mkt_no
            if edge < self.MIN_EDGE:
                return None
            side, token, mkt_p, fair_p = "NO", market.no_token, mkt_no, fair_no

        confidence = "HIGH" if abs(move) > 0.25 else "MEDIUM"
        logger.info(
            f"[NEWS-EVENT] {market.question[:55]}  "
            f"prev={prev:.3f}  now={current:.3f}  move={move:+.3f}  "
            f"→ {side} [{confidence}]"
        )
        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_p,
            fair_value=fair_p,
            edge=edge,
            signal=move,
            confidence=confidence,
            is_latency_arb=True,
        )
