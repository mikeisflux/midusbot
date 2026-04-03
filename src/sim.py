"""
SimPortfolio — full paper-trading simulation that mirrors every trade the bot
would take in live mode, using real market prices.

Separate from the AdaptiveLearner journal. Persists to data/journal_sim.json.
Tracks a virtual $100 wallet. Every open/close uses the real Polymarket price
(order book ask for entries, CLOB resolved price for exits).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loguru import logger
from src.utils import atomic_json_write

DATA_DIR = Path("data")
SIM_FILE  = DATA_DIR / "journal_sim.json"
STARTING_BALANCE = 150.00   # paper wallet size — update to match planned real deposit


@dataclass
class SimTrade:
    token_id:    str
    market_id:   str
    question:    str
    side:        str    # "YES" | "NO"
    entry_price: float
    shares:      float
    cost_usdc:   float
    opened_at:   float
    exit_price:  float = 0.0
    pnl_usdc:    float = 0.0
    closed_at:   float = 0.0
    closed:      bool  = False


class SimPortfolio:
    """
    Paper-trading portfolio.  Wire into bot._execute_signal and _process_sim_queue.
    All prices are REAL — only the starting balance is virtual.
    """

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._wallet: float               = STARTING_BALANCE
        self._open:   dict[str, SimTrade] = {}   # token_id → open trade
        self._closed: list[SimTrade]      = []
        self._equity: list[list]          = []   # [[ts_ms, balance], ...]
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_starting_balance(self, real_balance: float) -> None:
        """
        Align the sim wallet to the real wallet balance — only when the
        journal is fresh (no closed trades and wallet == STARTING_BALANCE).
        This lets the sim mirror exactly what the live wallet has today
        without resetting mid-session.
        """
        if real_balance <= 0:
            return
        if self._closed:
            return  # session already in progress — don't touch it
        if abs(self._wallet - STARTING_BALANCE) > 0.01:
            return  # journal was loaded with a different seed — leave it
        if abs(self._wallet - real_balance) < 0.01:
            return  # already in sync
        self._wallet = round(real_balance, 6)
        self._save()
        logger.info(f"[SIM] Wallet synced to real balance: ${self._wallet:.2f}")

    def reset_wallet(self, new_balance: float | None = None) -> dict:
        """
        Hard-reset the sim wallet.  Clears all open/closed history so paper-trading
        starts fresh.  Learning data in journal.json is NOT touched.

        Returns a summary dict of the session that just ended (for logging/learning).
        """
        if new_balance is None:
            new_balance = STARTING_BALANCE
        if new_balance <= 0:
            new_balance = STARTING_BALANCE

        # Build session summary before clearing
        closed      = self._closed
        wins        = [t for t in closed if t.pnl_usdc > 0]
        total_pnl   = sum(t.pnl_usdc for t in closed)
        summary = {
            "old_wallet":    self._wallet,
            "new_wallet":    round(new_balance, 2),
            "total_trades":  len(closed),
            "wins":          len(wins),
            "losses":        len(closed) - len(wins),
            "win_rate":      round(len(wins) / len(closed), 4) if closed else 0.0,
            "total_pnl":     round(total_pnl, 4),
            "open_at_reset": len(self._open),
            "reset_at":      time.time(),
        }

        old_wallet       = self._wallet
        self._wallet     = round(new_balance, 6)
        self._open.clear()
        self._closed.clear()
        self._equity.clear()
        self._save()
        logger.warning(
            f"[SIM] Wallet reset: ${old_wallet:.2f} → ${new_balance:.2f}  "
            f"session: {summary['total_trades']}T  "
            f"wr={summary['win_rate']:.1%}  pnl={summary['total_pnl']:+.2f}  "
            f"(paper trades cleared; learning data preserved)"
        )
        return summary

    def open_position(
        self, *,
        token_id:    str,
        market_id:   str,
        question:    str,
        side:        str,
        entry_price: float,
        shares:      float,
    ) -> bool:
        """
        Record a new sim position at the given entry_price (should be real ask).
        Returns False if already tracking this token or balance too low.
        """
        if token_id in self._open:
            return False
        cost = round(entry_price * shares, 6)
        if cost > self._wallet + 0.01:          # tiny float buffer
            logger.debug(f"[SIM] Insufficient balance ${self._wallet:.2f} for ${cost:.2f}")
            return False
        self._wallet = round(self._wallet - cost, 6)
        self._open[token_id] = SimTrade(
            token_id=token_id, market_id=market_id, question=question,
            side=side, entry_price=entry_price, shares=shares,
            cost_usdc=cost, opened_at=time.time(),
        )
        self._save()
        logger.info(
            f"[SIM] OPEN  {side} {shares:.2f}@{entry_price:.4f} = ${cost:.2f}  "
            f"bal=${self._wallet:.2f}  {question[:45]}"
        )
        return True

    def close_position(self, token_id: str, exit_price: float) -> float:
        """
        Close a sim position.  Returns realised P&L in USDC.
        exit_price should be the real resolved price (0.00 or 1.00 for binary).
        """
        trade = self._open.pop(token_id, None)
        if trade is None:
            return 0.0
        gross = exit_price * trade.shares
        pnl   = round(gross - trade.cost_usdc, 6)
        self._wallet = round(self._wallet + gross, 6)
        trade.exit_price = exit_price
        trade.pnl_usdc   = pnl
        trade.closed_at  = time.time()
        trade.closed     = True
        self._closed.append(trade)
        self._equity.append([int(time.time() * 1000), round(self._wallet, 4)])
        self._save()
        tag = "WIN " if pnl > 0 else "LOSS"
        logger.info(
            f"[SIM] {tag}  {trade.side} @{exit_price:.4f}  "
            f"P&L=${pnl:+.2f}  bal=${self._wallet:.2f}  {trade.question[:45]}"
        )
        return pnl

    def get_stats(self) -> dict:
        """Return a serialisable stats dict for the dashboard."""
        wins   = [t for t in self._closed if t.pnl_usdc > 0]
        losses = [t for t in self._closed if t.pnl_usdc <= 0]
        n      = len(self._closed)
        return {
            "wallet":           round(self._wallet, 4),
            "starting_balance": STARTING_BALANCE,
            "total_pnl":        round(self._wallet - STARTING_BALANCE, 4),
            "open_positions":   len(self._open),
            "total_trades":     n,
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate":         round(len(wins) / n, 4) if n else 0.0,
            "best_trade":       round(max((t.pnl_usdc for t in self._closed), default=0.0), 4),
            "worst_trade":      round(min((t.pnl_usdc for t in self._closed), default=0.0), 4),
            "equity_curve":     [{"t": p[0], "v": p[1]} for p in self._equity[-300:]],
            "recent_trades": [
                {
                    "question": t.question[:55],
                    "side":     t.side,
                    "entry":    round(t.entry_price, 4),
                    "exit":     round(t.exit_price, 4),
                    "pnl":      round(t.pnl_usdc, 4),
                    "win":      t.pnl_usdc > 0,
                    "ts":       int(t.closed_at),
                }
                for t in reversed(self._closed[-15:])
            ],
            "open_trades": [
                {
                    "question": t.question[:55],
                    "side":     t.side,
                    "entry":    round(t.entry_price, 4),
                    "cost":     round(t.cost_usdc, 4),
                    "ts":       int(t.opened_at),
                }
                for t in self._open.values()
            ],
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        try:
            payload = {
                "wallet":  self._wallet,
                "equity":  self._equity[-500:],
                "open":    [asdict(t) for t in self._open.values()],
                "closed":  [asdict(t) for t in self._closed[-500:]],
            }
            atomic_json_write(SIM_FILE, payload)
        except Exception as exc:
            logger.warning(f"[SIM] Save failed: {exc}")

    def _load(self) -> None:
        if not SIM_FILE.exists():
            logger.info(f"[SIM] No existing journal — starting fresh at ${STARTING_BALANCE:.2f}")
            return
        try:
            with open(SIM_FILE) as f:
                d = json.load(f)
            self._wallet = float(d.get("wallet", STARTING_BALANCE))
            self._equity = d.get("equity", [])
            fields = set(SimTrade.__dataclass_fields__)
            for raw in d.get("closed", []):
                self._closed.append(SimTrade(**{k: v for k, v in raw.items() if k in fields}))
            for raw in d.get("open", []):
                t = SimTrade(**{k: v for k, v in raw.items() if k in fields})
                self._open[t.token_id] = t
            logger.info(
                f"[SIM] Loaded: wallet=${self._wallet:.2f}  "
                f"closed={len(self._closed)}  open={len(self._open)}"
            )
        except Exception as exc:
            logger.warning(f"[SIM] Load failed: {exc} — starting fresh")
            self._wallet = STARTING_BALANCE
