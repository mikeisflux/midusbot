"""
Risk manager — position sizing and exposure control.

All tuneable constants (Kelly multiplier, stop-loss, take-profit) can be
overridden at runtime by the AdaptiveLearner through a RiskParams object.

Exposure is computed on-demand from the live positions dict (source of truth)
rather than maintained as a separate accumulator that can drift.
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
# Daily loss cap: how much of the bank can be lost before halting new trades.
# Dry-run uses a much looser cap — simulated losses are how the bot learns.
# Live trading uses a tight cap to protect real capital.
DEFAULT_DAILY_LOSS_CAP_LIVE    = -0.03   # -3% of wallet in live mode
DEFAULT_DAILY_LOSS_CAP_DRYRUN  = -0.20   # -20% of sim wallet in dry-run


class RiskManager:
    def __init__(self, params: RiskParams | None = None, positions: dict | None = None) -> None:
        self._params = params
        self._positions = positions if positions is not None else {}
        self._wallet_balance: float = 0.0
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
        """Returns the USDC to spend. 0.0 = skip trade."""
        # Daily loss breaker — always use MAX_TOTAL_EXPOSURE_USDC as the
        # reference so the cap stays stable as sim equity fluctuates.
        # A shrinking sim wallet would cause increasingly sensitive caps.
        ref = config.MAX_TOTAL_EXPOSURE_USDC
        daily_pnl_pct = self._daily_pnl / ref if ref else 0
        cap = DEFAULT_DAILY_LOSS_CAP_DRYRUN if config.DRY_RUN else DEFAULT_DAILY_LOSS_CAP_LIVE
        if daily_pnl_pct <= cap:
            logger.warning(f"Daily loss cap hit ({daily_pnl_pct:.1%}) — no new trades until reset.")
            return 0.0

        usdc = self._kelly_size(signal)
        if usdc <= 0:
            return 0.0

        # UpDown HIGH-confidence signals allow larger positions to mirror
        # reference traders (200+ shares). Cap scales with confidence.
        from src.strategy import _detect_updown_market
        is_updown = _detect_updown_market(signal.question) is not None
        if is_updown and signal.confidence == "HIGH":
            pos_cap = config.MAX_POSITION_USDC * 3.0
        elif is_updown and signal.confidence == "MEDIUM":
            pos_cap = config.MAX_POSITION_USDC * 1.5
        else:
            pos_cap = config.MAX_POSITION_USDC

        capped = min(usdc, pos_cap)

        # Exposure cap: MAX_EXPOSURE_PCT of wallet balance, or static fallback
        dynamic_cap = (
            self._wallet_balance * config.MAX_EXPOSURE_PCT
            if self._wallet_balance > 0
            else config.MAX_TOTAL_EXPOSURE_USDC
        )
        open_usdc = self.total_exposure()
        headroom = dynamic_cap - open_usdc
        if headroom <= 0:
            logger.debug(
                f"Exposure cap reached (cap={dynamic_cap:.2f} USDC, open={open_usdc:.2f}) — skipping."
            )
            return 0.0

        return round(min(capped, headroom), 2)

    def shares_from_usdc(self, usdc: float, price: float) -> float:
        if price <= 0:
            return 0.0
        shares = usdc / price
        if shares < config.MIN_ORDER_SHARES:
            return 0.0
        return round(shares, 2)

    def set_wallet_balance(self, balance: float) -> None:
        self._wallet_balance = max(0.0, balance)

    def record_open(self) -> None:
        """Call when a new trade is opened — tracks daily trade count."""
        self._trades_today += 1

    def record_close(self, pnl_usdc: float = 0.0) -> None:
        """Call when a trade is closed — tracks daily P&L."""
        self._daily_pnl += pnl_usdc

    def total_exposure(self) -> float:
        """Live open exposure in USDC — bot-placed positions only.

        External (reconciled) positions are excluded: they represent capital
        committed before the bot's risk management was active, so counting them
        would incorrectly block new bot-placed trades.
        """
        return sum(
            p.cost_usdc for p in self._positions.values()
            if not p.is_external
        )

    def should_stop_loss(self, pnl_pct: float) -> bool:
        return pnl_pct <= self._stop_loss

    def should_take_profit(self, pnl_pct: float) -> bool:
        return pnl_pct >= self._take_profit

    def daily_pnl(self) -> float:
        return self._daily_pnl

    def trade_fee(self, entry_usdc: float, exit_usdc: float = 0.0) -> float:
        """Round-trip cost: maker fee + gas."""
        fee = (entry_usdc + exit_usdc) * config.MAKER_FEE_PCT
        gas = 2 * config.GAS_COST_USDC
        return round(fee + gas, 4)

    def reset_daily(self) -> None:
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

        # Allow LLM analyst to override kelly fraction
        try:
            from src.analyst import load_params as _lp
            _ko = _lp().get("kelly_override")
            kelly_cfg = float(_ko) if _ko is not None else config.KELLY_FRACTION
        except Exception:
            kelly_cfg = config.KELLY_FRACTION

        # MAX_TOTAL_EXPOSURE_USDC is the intended portfolio bank for Kelly sizing.
        # The wallet balance cap is enforced separately in position_size().
        return float(
            kelly_fraction
            * kelly_cfg
            * self._kelly_mult
            * config.MAX_TOTAL_EXPOSURE_USDC
        )
