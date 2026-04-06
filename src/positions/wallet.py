"""
Wallet sync and daily loss monitoring.

WalletMixin provides:
  _sync_wallet_balance      — fetch live USDC balance and update risk manager
  _check_wallet_replenishment — alert when balance drops below threshold
  _check_daily_loss_alert   — alert when daily P&L crosses -10% threshold
"""
from __future__ import annotations

from loguru import logger

import config


class WalletMixin:
    _DAILY_LOSS_ALERT_PCT: float = 0.10
    _daily_loss_alerted:   bool  = False

    def _sync_wallet_balance(self) -> None:
        balance = self._client.get_usdc_balance()
        if balance is not None and balance > 0:
            self._dash_state.wallet_balance = balance
            # Seed is set once from STARTING_WALLET_USDC config (your deposit).
            # Only fall back to current wallet if config is not set.
            if self._dash_state._seed <= config.MAX_TOTAL_EXPOSURE_USDC:
                _cfg_start = getattr(config, "STARTING_WALLET_USDC", 0.0)
                self._dash_state._seed = _cfg_start if _cfg_start > 0 else balance
            logger.info(f"Wallet balance: ${balance:.2f} USDC")
            self._dash_state.add_exec_log("info", f"Wallet: ${balance:.2f} USDC")

        if config.DRY_RUN:
            sim_open_cost = sum(t.cost_usdc for t in self._sim._open.values())
            sim_equity = self._sim._wallet + sim_open_cost
            self._check_sim_loss_limit(sim_equity)
            sim_equity = self._sim._wallet + sum(t.cost_usdc for t in self._sim._open.values())
            if sim_equity > 0:
                self._risk.set_wallet_balance(sim_equity)
        elif balance is not None and balance > 0:
            self._risk.set_wallet_balance(balance)
        else:
            logger.debug("Wallet balance unavailable (no auth or dry-run)")

        self._check_wallet_replenishment()

    def _check_wallet_replenishment(self) -> None:
        """Alert when wallet balance drops below threshold — needs USDC top-up."""
        balance = self._dash_state.wallet_balance
        if balance <= 0:
            return
        _REPLENISH_THRESHOLD = config.WALLET_REPLENISH_ALERT
        if not hasattr(self, "_replenish_alerted"):
            self._replenish_alerted = False
        if balance < _REPLENISH_THRESHOLD and not self._replenish_alerted:
            from src.utils import alerter
            alerter.send(
                f"⚠️ Wallet balance ${balance:.2f} USDC — below ${_REPLENISH_THRESHOLD:.0f} threshold. "
                f"Please bridge USDC to Polygon to continue trading.",
                level="warning",
            )
            self._replenish_alerted = True
        elif balance >= _REPLENISH_THRESHOLD:
            self._replenish_alerted = False  # reset once topped up

    def _check_daily_loss_alert(self) -> None:
        from src.utils import alerter
        wallet = self._dash_state.wallet_balance or self._dash_state._seed
        if wallet <= 0:
            return
        daily_pnl = self._dash_state.daily_pnl
        loss_pct   = daily_pnl / wallet
        if loss_pct < -self._DAILY_LOSS_ALERT_PCT and not self._daily_loss_alerted:
            self._daily_loss_alerted = True
            alerter.send(
                f"Daily loss alert: P&L ${daily_pnl:.2f} ({loss_pct:.1%}) "
                f"on ${wallet:.2f} wallet",
                level="critical",
            )
        elif loss_pct >= 0:
            self._daily_loss_alerted = False
