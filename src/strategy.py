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

# External price feeds (tried in order; first success wins)
_BTC_FEEDS = [
    ("CoinGecko",    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"),
    ("Binance",      "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
]

_PRICE_CACHE: dict[str, tuple[float, float]] = {}   # symbol → (price, timestamp)
_CACHE_TTL = 2.0   # seconds


def _fetch_btc_price() -> float | None:
    """Best-effort BTC/USD price from public APIs (cached 2 s)."""
    cached = _PRICE_CACHE.get("BTC")
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]

    for name, url in _BTC_FEEDS:
        try:
            resp = requests.get(url, timeout=3)
            resp.raise_for_status()
            data = resp.json()
            if "bitcoin" in data:
                price = float(data["bitcoin"]["usd"])
            elif "price" in data:
                price = float(data["price"])
            else:
                continue
            _PRICE_CACHE["BTC"] = (price, time.time())
            return price
        except Exception:
            continue

    return None


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
