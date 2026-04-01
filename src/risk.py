"""
Risk manager — position sizing and exposure control.

Position sizing
───────────────
Uses fractional Kelly Criterion:

    f* = (b·p − q) / b

where
  p  = estimated probability of winning (our fair_value)
  q  = 1 − p
  b  = net odds = (1 / market_price) − 1   (profit per USDC staked)

  Kelly fraction (KELLY_FRACTION) is applied on top (e.g. 0.25 = quarter-Kelly).

The raw Kelly stake is then capped by:
  • MAX_POSITION_USDC per trade
  • Remaining headroom in MAX_TOTAL_EXPOSURE_USDC

Stop-loss / take-profit
───────────────────────
Checked every loop iteration.  Positions are flagged for closure when:
  • PnL% ≤ −STOP_LOSS_PCT  (default −50 %)
  • PnL% ≥  TAKE_PROFIT_PCT (default +80 %)
"""
from __future__ import annotations

import numpy as np
from loguru import logger

from src.strategy import TradeSignal
import config

# ---------------------------------------------------------------------------
# Constants (could be moved to config if you want env-var control)
# ---------------------------------------------------------------------------
STOP_LOSS_PCT   = -0.50   # close if position is down 50 %
TAKE_PROFIT_PCT =  0.80   # close if position is up 80 %


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    def __init__(self) -> None:
        self._open_cost: float = 0.0   # total USDC currently at risk

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def position_size(self, signal: TradeSignal) -> float:
        """
        Returns the number of USDC to spend on this trade (i.e. shares × price).
        Returns 0.0 if the trade should be skipped.
        """
        kelly_usdc = self._kelly_size(signal)
        if kelly_usdc <= 0:
            return 0.0

        # Apply hard caps
        capped = min(kelly_usdc, config.MAX_POSITION_USDC)

        # Remaining exposure budget
        headroom = config.MAX_TOTAL_EXPOSURE_USDC - self._open_cost
        if headroom <= 0:
            logger.warning("Total exposure limit reached — skipping new trades.")
            return 0.0

        usdc_to_spend = min(capped, headroom)
        logger.debug(
            f"Kelly stake: {kelly_usdc:.2f} USDC → capped: {capped:.2f} "
            f"→ after headroom: {usdc_to_spend:.2f} USDC"
        )
        return round(usdc_to_spend, 2)

    def shares_from_usdc(self, usdc: float, price: float) -> float:
        """Convert a USDC amount to number of shares at the given price."""
        if price <= 0:
            return 0.0
        return round(usdc / price, 2)

    def register_open(self, usdc_cost: float) -> None:
        """Call after a position is opened."""
        self._open_cost += usdc_cost
        logger.debug(f"Registered open position: ${usdc_cost:.2f}  total={self._open_cost:.2f}")

    def register_close(self, usdc_cost: float) -> None:
        """Call after a position is closed."""
        self._open_cost = max(0.0, self._open_cost - usdc_cost)
        logger.debug(f"Registered closed position: ${usdc_cost:.2f}  total={self._open_cost:.2f}")

    def should_stop_loss(self, pnl_pct: float) -> bool:
        return pnl_pct <= STOP_LOSS_PCT

    def should_take_profit(self, pnl_pct: float) -> bool:
        return pnl_pct >= TAKE_PROFIT_PCT

    def total_exposure(self) -> float:
        return self._open_cost

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _kelly_size(self, signal: TradeSignal) -> float:
        """Compute fractional-Kelly stake in USDC."""
        p = float(np.clip(signal.fair_value, 0.01, 0.99))  # prob of winning
        q = 1.0 - p
        mkt = float(np.clip(signal.market_price, 0.01, 0.99))
        b = (1.0 / mkt) - 1.0  # net odds

        if b <= 0:
            return 0.0

        kelly_fraction = (b * p - q) / b
        if kelly_fraction <= 0:
            logger.debug(f"Negative Kelly ({kelly_fraction:.4f}) — no edge, skip.")
            return 0.0

        stake = kelly_fraction * config.KELLY_FRACTION * config.MAX_TOTAL_EXPOSURE_USDC
        return float(stake)
