"""
Trading strategy — Momentum + Order-Book Imbalance.

Signal logic
────────────
For every active market we compute two sub-signals, each in [-1, +1]:

  1. Momentum signal
     • Compare the current YES price to the price 24 h ago (from hourly
       Gamma history).
     • Positive → price is rising → bullish on YES.

  2. Order-book imbalance signal
     • Sum bid quantity vs. ask quantity in the top N levels.
     • Positive → more buy pressure than sell pressure → bullish on YES.

The composite signal is the equal-weight average of the two.  A signal above
+THRESHOLD triggers a BUY on the YES token; below -THRESHOLD triggers a BUY
on the NO token (i.e. we think YES will lose).

Edge requirement
────────────────
Before trading we also check that the current market price gives us enough
"edge" relative to our estimated fair value.  If the edge is smaller than
MIN_EDGE the trade is skipped even if the composite signal is strong.

Fair-value estimate
───────────────────
  fair_value = mid_price * (1 + signal * MAX_SIGNAL_ADJUST)

where MAX_SIGNAL_ADJUST caps the adjustment at ±7 %.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from loguru import logger

from src.client import Market, OrderBook, PricePoint
import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIGNAL_THRESHOLD = 0.15       # composite signal must exceed this to trade
MAX_SIGNAL_ADJUST = 0.07      # max ±7 % price adjustment from signals
OB_LEVELS = 10                # how many order-book levels to include
MOMENTUM_NORMALISER = 0.20    # 20 % price move maps to ±1 signal


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class TradeSignal:
    market_id: str
    question: str
    side: str           # "YES" | "NO"
    token_id: str
    market_price: float  # current market mid price for the chosen token
    fair_value: float    # our estimated fair price
    edge: float          # fair_value - market_price (positive = we have edge)
    signal: float        # composite signal [-1, 1]
    confidence: str      # "LOW" | "MEDIUM" | "HIGH"

    def __str__(self) -> str:
        return (
            f"[{self.confidence}] {self.side} {self.question[:60]} | "
            f"mkt={self.market_price:.3f}  fv={self.fair_value:.3f}  "
            f"edge={self.edge:+.3f}  sig={self.signal:+.3f}"
        )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class MomentumImbalanceStrategy:
    """
    Scans a list of markets and returns TradeSignal objects for markets where
    the composite signal is strong enough and the edge exceeds MIN_EDGE.
    """

    def analyse(
        self,
        market: Market,
        order_book: OrderBook | None,
        price_history: list[PricePoint],
    ) -> TradeSignal | None:
        """
        Returns a TradeSignal if an opportunity is found, else None.
        """
        try:
            momentum_sig = self._momentum_signal(market, price_history)
            imbalance_sig = self._imbalance_signal(order_book)

            # Equal-weight composite
            composite = 0.5 * momentum_sig + 0.5 * imbalance_sig

            logger.debug(
                f"{market.question[:50]} | mom={momentum_sig:+.3f}  "
                f"imb={imbalance_sig:+.3f}  composite={composite:+.3f}"
            )

            if abs(composite) < SIGNAL_THRESHOLD:
                return None

            # Choose direction
            if composite > 0:
                # Bullish on YES
                side = "YES"
                token = market.yes_token
                mid = order_book.mid if order_book else market.yes_price
            else:
                # Bearish on YES = bullish on NO
                side = "NO"
                token = market.no_token
                mid = (1.0 - order_book.mid) if order_book else market.no_price
                composite = -composite  # flip so edge calc makes sense

            # Fair value with signal adjustment
            fair_value = mid * (1.0 + composite * MAX_SIGNAL_ADJUST)
            fair_value = float(np.clip(fair_value, 0.01, 0.99))

            edge = fair_value - mid

            if edge < config.MIN_EDGE:
                logger.debug(
                    f"  → edge {edge:.3f} below MIN_EDGE {config.MIN_EDGE:.3f}, skip"
                )
                return None

            confidence = (
                "HIGH"   if abs(composite) > 0.5 else
                "MEDIUM" if abs(composite) > 0.3 else
                "LOW"
            )

            signal = TradeSignal(
                market_id=market.id,
                question=market.question,
                side=side,
                token_id=token.token_id,
                market_price=mid,
                fair_value=fair_value,
                edge=edge,
                signal=composite,
                confidence=confidence,
            )
            logger.info(f"  → Signal: {signal}")
            return signal

        except Exception as exc:
            logger.warning(f"Strategy error on market {market.id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Sub-signals
    # ------------------------------------------------------------------

    def _momentum_signal(
        self, market: Market, price_history: list[PricePoint]
    ) -> float:
        """
        Returns a value in [-1, 1].
        Uses 24-h price change if history available, else 0.
        """
        if len(price_history) < 2:
            return 0.0

        # price_history is chronological; last element is most recent
        recent = price_history[-1].price
        # ~24 h ago: with hourly fidelity that's ~24 points back
        lookback = min(24, len(price_history) - 1)
        past = price_history[-1 - lookback].price

        if past <= 0:
            return 0.0

        change = (recent - past) / past
        return float(np.clip(change / MOMENTUM_NORMALISER, -1.0, 1.0))

    def _imbalance_signal(self, ob: OrderBook | None) -> float:
        """
        Returns a value in [-1, 1].
        Positive = more bid volume → buying pressure on YES.
        """
        if ob is None:
            return 0.0

        bid_qty = sum(
            float(b.get("size", 0)) for b in ob.bids[:OB_LEVELS]
        )
        ask_qty = sum(
            float(a.get("size", 0)) for a in ob.asks[:OB_LEVELS]
        )
        total = bid_qty + ask_qty
        if total == 0:
            return 0.0

        return float((bid_qty - ask_qty) / total)
