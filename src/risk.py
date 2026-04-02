"""
Risk manager — position sizing and exposure control.

All tuneable constants (Kelly multiplier, stop-loss, take-profit) can be
overridden at runtime by the AdaptiveLearner through a RiskParams object.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from src.strategy import TradeSignal
import config

if TYPE_CHECKING:
    from src.learner import RiskParams

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_STOP_LOSS_PCT   = -0.50
DEFAULT_TAKE_PROFIT_PCT =  0.80
DEFAULT_KELLY_MULT      =  1.0
DEFAULT_DAILY_LOSS_CAP  = -0.02  # stop trading if daily P&L < -2 %


class RiskManager:
    def __init__(self, params: RiskParams | None = None) -> None:
        self._params = params
        self._open_cost: float = 0.0
        # Daily P&L tracking
        self._daily_pnl: float = 0.0
        self._trades_today: int = 0

    # -- adaptive getters --------------------------------------------------

    @property
    def _stop_loss(self) -> float:
        return self._params.stop_loss_pct if self._params else DEFAULT_STOP_LOSS_PCT

    @property
    def _take_profit(self) -> float:
        return self._params.take_profit_pct if self._params else DEFAULT_TAKE_PROFIT_PCT

    @property
    def _kelly_mult(self) -> float:
        return self._params.kelly_multiplier if self._params else DEFAULT_KELLY_MULT

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def position_size(self, signal: TradeSignal) -> float:
        """
        Returns the USDC to spend.  0.0 = skip trade.
        Latency-arb trades use a fixed risk budget (0.5 % of exposure cap).
        """
        # Daily loss breaker
        daily_pnl_pct = self._daily_pnl / config.MAX_TOTAL_EXPOSURE_USDC if config.MAX_TOTAL_EXPOSURE_USDC else 0
        if daily_pnl_pct <= DEFAULT_DAILY_LOSS_CAP:
            logger.warning(
                f"Daily loss cap hit ({daily_pnl_pct:.1%}) — no new trades until reset."
            )
            return 0.0

        if signal.is_latency_arb:
            # Fixed fractional risk for arb: 0.5 % of total exposure cap
            usdc = config.MAX_TOTAL_EXPOSURE_USDC * 0.005
        else:
            usdc = self._kelly_size(signal)

        if usdc <= 0:
            return 0.0

        # UpDown HIGH-confidence signals mirror the reference trader's large
        # positions (200+ shares). Allow up to 3× MAX_POSITION_USDC when
        # momentum is very strong and fair_value is well above market price.
        from src.strategy import _detect_updown_market
        is_updown = _detect_updown_market(signal.question) is not None
        if is_updown and signal.confidence == "HIGH":
            pos_cap = config.MAX_POSITION_USDC * 3.0
        elif is_updown and signal.confidence == "MEDIUM":
            pos_cap = config.MAX_POSITION_USDC * 1.5
        else:
            pos_cap = config.MAX_POSITION_USDC

        capped = min(usdc, pos_cap)

        headroom = config.MAX_TOTAL_EXPOSURE_USDC - self._open_cost
        if headroom <= 0:
            logger.warning("Total exposure limit reached — skipping.")
            return 0.0

        result = min(capped, headroom)
        return round(result, 2)

    def shares_from_usdc(self, usdc: float, price: float) -> float:
        if price <= 0:
            return 0.0
        shares = usdc / price
        # Polymarket minimum is 5 shares — return 0 so the caller skips this trade
        if shares < config.MIN_ORDER_SHARES:
            return 0.0
        return round(shares, 2)

    def register_open(self, usdc_cost: float) -> None:
        self._open_cost += usdc_cost
        self._trades_today += 1

    def register_close(self, usdc_cost: float, pnl_usdc: float = 0.0) -> None:
        self._open_cost = max(0.0, self._open_cost - usdc_cost)
        self._daily_pnl += pnl_usdc

    def should_stop_loss(self, pnl_pct: float) -> bool:
        return pnl_pct <= self._stop_loss

    def should_take_profit(self, pnl_pct: float) -> bool:
        return pnl_pct >= self._take_profit

    def total_exposure(self) -> float:
        return self._open_cost

    def daily_pnl(self) -> float:
        return self._daily_pnl

    def trade_fee(self, entry_usdc: float, exit_usdc: float = 0.0) -> float:
        """
        Round-trip transaction cost: maker fee on entry + exit notional,
        plus two Polygon gas transactions (~$0.02 each by default).
        Polymarket CLOB currently charges 0% fees, so cost ≈ gas only.
        """
        fee = (entry_usdc + exit_usdc) * config.MAKER_FEE_PCT
        gas = 2 * config.GAS_COST_USDC
        return round(fee + gas, 4)

    def reset_daily(self) -> None:
        """Call at midnight to reset daily P&L tracking."""
        self._daily_pnl = 0.0
        self._trades_today = 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _kelly_size(self, signal: TradeSignal) -> float:
        p = float(np.clip(signal.fair_value, 0.01, 0.99))
        q = 1.0 - p
        mkt = float(np.clip(signal.market_price, 0.01, 0.99))
        b = (1.0 / mkt) - 1.0

        if b <= 0:
            return 0.0

        kelly_fraction = (b * p - q) / b
        if kelly_fraction <= 0:
            return 0.0

        stake = (
            kelly_fraction
            * config.KELLY_FRACTION
            * self._kelly_mult          # adaptive multiplier from learner
            * config.MAX_TOTAL_EXPOSURE_USDC
        )
        return float(stake)
