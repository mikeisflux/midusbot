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
        # Internal exposure counter — incremented on open, decremented on close.
        # Never reads from _positions or the CLOB API so it can't drift/freeze.
        self._open_exposure: float = 0.0

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
        # Daily trading floor: halt if live wallet balance drops below the
        # configured floor. Recovers automatically when wins push balance back up.
        # Only active when wallet balance is known (>0); falls back to pct cap otherwise.
        if self._wallet_balance > 0:
            if self._wallet_balance < config.DAILY_LOSS_FLOOR_USDC:
                logger.warning(
                    f"Daily floor hit — wallet ${self._wallet_balance:.2f} < "
                    f"${config.DAILY_LOSS_FLOOR_USDC:.0f} floor — no new trades until balance recovers."
                )
                return 0.0

            # Circuit breaker: pause all new entries if daily PnL falls below
            # CIRCUIT_BREAKER_PCT (default -5%) of current wallet balance.
            _cb_pct = float(getattr(config, "CIRCUIT_BREAKER_PCT", -0.05))
            _daily_pnl_pct = self._daily_pnl / self._wallet_balance if self._wallet_balance else 0
            if _daily_pnl_pct <= _cb_pct:
                logger.warning(
                    f"[CIRCUIT-BREAKER] Daily PnL {_daily_pnl_pct:.1%} ≤ {_cb_pct:.0%} — "
                    f"all new entries paused until daily reset."
                )
                return 0.0
        else:
            # Wallet not yet synced — fall back to percentage cap on daily P&L
            ref = config.MAX_TOTAL_EXPOSURE_USDC
            daily_pnl_pct = self._daily_pnl / ref if ref else 0
            cap = DEFAULT_DAILY_LOSS_CAP_DRYRUN if config.DRY_RUN else DEFAULT_DAILY_LOSS_CAP_LIVE
            if daily_pnl_pct <= cap:
                logger.warning(f"Daily loss cap hit ({daily_pnl_pct:.1%}) — no new trades until reset.")
                return 0.0

        # Multi-asset correlated exposure cap: limit total exposure across BTC/ETH/BNB/SOL/XRP/DOGE/HYPE.
        # These assets are 90%+ correlated — simultaneous exposure multiplies directional risk.
        _CORRELATED_GROUP = {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "HYPE"}
        if self._wallet_balance > 0:
            _group_cap = self._wallet_balance * config.GROUP_CAP_PCT
        else:
            _group_cap = config.MAX_TOTAL_EXPOSURE_USDC * 0.5

        from src.strategy import _detect_updown_market
        _signal_sym = _detect_updown_market(getattr(signal, "question", ""))
        if _signal_sym and _signal_sym.upper() in _CORRELATED_GROUP:
            if self._open_exposure >= _group_cap:
                logger.debug(
                    f"[GROUP-CAP] Correlated asset cap reached "
                    f"(open={self._open_exposure:.2f} >= cap={_group_cap:.2f}) — skip"
                )
                return 0.0

        usdc = self._kelly_size(signal)
        if usdc <= 0:
            return 0.0

        # Floor: if Kelly produces less than the minimum viable trade size,
        # bump up to the floor so the signal actually fires. A minimum-size
        # trade at positive edge is better than no trade.
        _min_trade = config.MIN_ORDER_SHARES * 0.52  # 5 shares × $0.52 buffer
        if usdc < _min_trade and signal.edge > 0:
            usdc = _min_trade

        # UpDown HIGH-confidence signals allow larger positions to mirror
        # reference traders (200+ shares). Cap scales with confidence.
        # Bankroll-proportional: effective max = 12% of known wallet balance,
        # but never exceeds config.MAX_POSITION_USDC (protects during drawdown).
        effective_max = (
            min(self._wallet_balance * config.POSITION_WALLET_PCT, config.MAX_POSITION_USDC)
            if self._wallet_balance > 0
            else config.MAX_POSITION_USDC
        )
        from src.strategy import _detect_updown_market
        is_updown = _detect_updown_market(signal.question) is not None
        if is_updown and signal.confidence == "HIGH":
            pos_cap = effective_max * 3.0
        elif is_updown and signal.confidence == "MEDIUM":
            pos_cap = effective_max * 1.5
        else:
            pos_cap = effective_max

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

    def record_open(self, cost_usdc: float = 0.0) -> None:
        """Call when a new trade is opened — tracks count and exposure."""
        self._trades_today += 1
        self._open_exposure = round(self._open_exposure + cost_usdc, 4)

    def record_close(self, pnl_usdc: float = 0.0, cost_usdc: float = 0.0) -> None:
        """Call when a trade is closed — tracks daily P&L and frees exposure."""
        self._daily_pnl += pnl_usdc
        # Free the capital that was deployed for this position.
        self._open_exposure = round(max(0.0, self._open_exposure - cost_usdc), 4)

    def total_exposure(self) -> float:
        """Live open exposure — purely internal counter, never reads from CLOB.

        Incremented by record_open(), decremented by record_close().
        Cannot drift or freeze due to API inconsistencies.
        """
        return self._open_exposure

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

        # Polymarket charges a fee on winnings — adjust payout ratio.
        # True payout = (1/mkt - 1) * (1 - fee) = b * (1 - fee)
        # This makes the breakeven accuracy slightly above 50%.
        b = b * (1.0 - config.POLY_WIN_FEE)

        kelly_fraction = (b * p - q) / b
        if kelly_fraction <= 0:
            return 0.0

        # Per-asset Kelly scaling from session tracker accuracy.
        # Kelly = 2 * signal_accuracy - 1 (full Kelly formula for binary bets).
        # Blend calculated Kelly with session-based Kelly when >= 30 windows seen.
        try:
            from src.session_tracker import get_asset_stats
            from src.strategy import _detect_updown_market
            _sym = _detect_updown_market(getattr(signal, "question", ""))
            if _sym:
                _stats = get_asset_stats(_sym, lookback=60)
                if _stats and _stats.get("signal_windows", 0) >= 30:
                    _acc = _stats.get("signal_accuracy", 0.5)
                    _session_kelly = max(0.0, 2 * _acc - 1)
                    # Blend: 50% calculated Kelly, 50% session-based Kelly
                    kelly_fraction = (kelly_fraction + _session_kelly) / 2
                    logger.debug(
                        f"[KELLY] {_sym} session_acc={_acc:.1%} → blend kelly={kelly_fraction:.3f}"
                    )
        except Exception:
            pass

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

    def risk_of_ruin(self, win_rate: float, kelly_fraction: float, n_bets: int = 100) -> float:
        """
        Compute approximate probability of ruin (losing all capital) using
        the gambler's ruin formula for fixed-fraction betting.

        win_rate: probability of winning each bet (0.0-1.0)
        kelly_fraction: fraction of bankroll bet each time
        n_bets: number of bets to simulate over

        Returns probability of ruin (0.0-1.0).
        """
        if win_rate <= 0 or win_rate >= 1:
            return 1.0 if win_rate <= 0 else 0.0
        if kelly_fraction <= 0:
            return 0.0
        # Monte Carlo approximation (1000 paths)
        import random
        ruins = 0
        n_sims = 1000
        for _ in range(n_sims):
            bankroll = 1.0
            for _ in range(n_bets):
                bet = bankroll * kelly_fraction
                if random.random() < win_rate:
                    bankroll += bet  # win
                else:
                    bankroll -= bet  # loss
                if bankroll <= 0.1:  # ruined (< 10% of start)
                    ruins += 1
                    break
        return ruins / n_sims
