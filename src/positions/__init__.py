"""
src/positions — position management package.

Sub-modules:
  store        — file paths and low-level JSON helpers
  persistence  — _save_positions, _load_positions, _mark_market_closed, _already_positioned
  monitoring   — _manage_positions, _gamma_position_price
  execution    — _execute_signal, _close_position
  reconcile    — _reconcile_positions
  wallet       — _sync_wallet_balance, _check_wallet_replenishment, _check_daily_loss_alert

PositionsMixin composes all sub-mixins into a single class.  Inheriting classes
(PolymarketBot) get the full position management API via a single import.

Backward-compatible helpers are re-exported at package level for callers like
bot.py that do `from src.positions import _load_closed_market_ids`.
"""
from __future__ import annotations

from src.positions.store import (
    load_closed_market_ids as _load_closed_market_ids,
    save_closed_market_ids as _save_closed_market_ids,
    load_redeemed          as _load_redeemed,
    save_redeemed          as _save_redeemed,
    _POSITIONS_FILE,
    _REDEEMED_FILE,
    _CLOSED_MARKETS_FILE,
)
from src.positions.persistence import PersistenceMixin
from src.positions.monitoring  import MonitoringMixin
from src.positions.execution   import ExecutionMixin
from src.positions.reconcile   import ReconcileMixin
from src.positions.wallet      import WalletMixin


class PositionsMixin(
    PersistenceMixin,
    MonitoringMixin,
    ExecutionMixin,
    ReconcileMixin,
    WalletMixin,
):
    """
    Full position management mixin — compose PersistenceMixin, MonitoringMixin,
    ExecutionMixin, ReconcileMixin, and WalletMixin into a single interface.

    Class-level state that sub-mixins reference:
      _redeemed_tokens  — lazy-loaded set[str]; None until first reconcile
    """
    _redeemed_tokens: set = None  # type: ignore[assignment]  — initialised lazily


__all__ = [
    "PositionsMixin",
    # Legacy helpers used by bot.py
    "_load_closed_market_ids",
    "_save_closed_market_ids",
    "_load_redeemed",
    "_save_redeemed",
    "_POSITIONS_FILE",
    "_REDEEMED_FILE",
    "_CLOSED_MARKETS_FILE",
]
