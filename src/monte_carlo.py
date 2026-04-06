"""
Monte Carlo position sizing — replaces fixed 12% Kelly fraction.

Simulates 1000 random paths of N trades forward from the current win-rate and
average P&L distribution, then picks the bet size that maximises terminal
geometric mean while keeping ruin probability (balance < 25% of start) below
MAX_RUIN_PROB (5%).

Used by risk.py `position_size()` when MC_SIZING_ENABLED=true (default: true).

Key parameters (all overridable in config):
  MC_SIMULATIONS   = 1000     — paths per sizing call
  MC_HORIZON       = 50       — trades to simulate forward
  MC_RUIN_FLOOR    = 0.25     — fraction of starting balance = ruin
  MAX_RUIN_PROB    = 0.05     — reject sizing levels above this ruin rate
  MC_SIZING_ENABLED = True    — toggle; falls back to Kelly when disabled
"""
from __future__ import annotations

import math
import random
import time
from typing import Optional

from loguru import logger


# ── Defaults (overridden by config if MC_* vars present) ──────────────────────
_MC_SIMULATIONS  = 1000
_MC_HORIZON      = 50
_MC_RUIN_FLOOR   = 0.25   # balance below 25% starting = ruin
_MAX_RUIN_PROB   = 0.05   # max acceptable ruin probability
_MC_MAX_FRACTION = 0.25   # hard cap regardless of MC result


def _get_cfg(key: str, default: float) -> float:
    try:
        import config as _cfg
        return float(getattr(_cfg, key, default))
    except Exception:
        return default


def optimal_fraction(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    wallet: float,
    *,
    simulations: Optional[int] = None,
    horizon:     Optional[int] = None,
) -> float:
    """
    Monte Carlo optimal Kelly fraction.

    win_rate  — historical win rate (0-1)
    avg_win   — average winning P&L as fraction of bet (e.g. 0.96 for 96% return)
    avg_loss  — average losing P&L as fraction of bet (e.g. -1.0 for full loss)
    wallet    — current wallet balance in USDC

    Returns a fraction of wallet (0.0–MC_MAX_FRACTION) to bet.
    """
    import config as _cfg
    if not getattr(_cfg, "MC_SIZING_ENABLED", True):
        return _get_cfg("POSITION_WALLET_PCT", 0.12)

    n_sims   = simulations or int(_get_cfg("MC_SIMULATIONS", _MC_SIMULATIONS))
    horizon  = horizon     or int(_get_cfg("MC_HORIZON",     _MC_HORIZON))
    ruin_floor = _get_cfg("MC_RUIN_FLOOR", _MC_RUIN_FLOOR)
    max_ruin   = _get_cfg("MAX_RUIN_PROB", _MAX_RUIN_PROB)
    max_frac   = _get_cfg("MC_MAX_FRACTION", _MC_MAX_FRACTION)

    # Guard against degenerate inputs
    if win_rate <= 0 or win_rate >= 1:
        return _get_cfg("POSITION_WALLET_PCT", 0.12)
    if avg_win <= 0 or avg_loss >= 0:
        return _get_cfg("POSITION_WALLET_PCT", 0.12)
    if wallet <= 0:
        return 0.0

    ruin_threshold = wallet * ruin_floor

    best_fraction = _get_cfg("POSITION_WALLET_PCT", 0.12)
    best_geomean  = 0.0

    # Sweep fractions from 0.02 to max_frac in 0.01 steps
    fractions = [round(f * 0.01, 3) for f in range(2, int(max_frac * 100) + 1)]

    t0 = time.time()
    for frac in fractions:
        bet_usdc = wallet * frac
        if bet_usdc < 1.0:
            continue

        ruin_count = 0
        terminal_log_sum = 0.0

        for _ in range(n_sims):
            bal = wallet
            for _ in range(horizon):
                if bal < ruin_threshold:
                    ruin_count += 1
                    bal = 0.0
                    break
                bet = bal * frac
                if random.random() < win_rate:
                    bal += bet * avg_win
                else:
                    bal += bet * avg_loss   # avg_loss is negative
            if bal > 0:
                terminal_log_sum += math.log(max(bal, 0.001) / wallet)

        ruin_prob = ruin_count / n_sims
        if ruin_prob > max_ruin:
            break  # fractions only get larger — all higher fractions also fail

        geomean = terminal_log_sum / (n_sims - ruin_count) if (n_sims - ruin_count) > 0 else 0.0
        if geomean > best_geomean:
            best_geomean  = geomean
            best_fraction = frac

    elapsed = (time.time() - t0) * 1000
    logger.debug(
        f"[MC] optimal fraction={best_fraction:.2%}  "
        f"geomean_log={best_geomean:.4f}  "
        f"sims={n_sims}×{horizon}  elapsed={elapsed:.0f}ms"
    )
    return best_fraction


def fraction_from_journal(journal: list, wallet: float) -> float:
    """
    Convenience wrapper: extract win_rate + avg P&L from a learner journal
    and call optimal_fraction().

    Falls back to POSITION_WALLET_PCT if insufficient data (<20 closed trades).
    """
    closed = [r for r in journal if getattr(r, "closed", False)]
    if len(closed) < 20:
        return _get_cfg("POSITION_WALLET_PCT", 0.12)

    recent = closed[-100:]   # last 100 trades
    wins   = [r for r in recent if getattr(r, "pnl_usdc", 0) > 0]
    losses = [r for r in recent if getattr(r, "pnl_usdc", 0) <= 0]

    win_rate = len(wins) / len(recent)

    # Express P&L as fraction of cost (stake)
    def _pnl_frac(r):
        cost = getattr(r, "cost_usdc", None) or (
            getattr(r, "entry_price", 0.5) * getattr(r, "shares", 1.0)
        )
        return getattr(r, "pnl_usdc", 0) / cost if cost > 0 else 0.0

    avg_win  = sum(_pnl_frac(r) for r in wins)  / len(wins)  if wins  else 0.90
    avg_loss = sum(_pnl_frac(r) for r in losses) / len(losses) if losses else -1.0

    return optimal_fraction(
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        wallet=wallet,
    )
