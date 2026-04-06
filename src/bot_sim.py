"""
Simulation queue management — mixin for PolymarketBot.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from loguru import logger

import config


class SimMixin:
    _SIM_LOSS_RESET_PCT: float = 0.20
    _SIM_MIN_TRADEABLE:  float = 2.50

    def _queue_sim(self, sig, entry: float, shares: float) -> None:
        now = time.time()
        if sig.hours_to_close is not None and 0 < sig.hours_to_close <= 24:
            close_after = now + sig.hours_to_close * 3600 + 90
        else:
            close_after = now + 300 + 90

        self._sim_queue.append({
            "question":        sig.question,
            "market_id":       sig.market_id,
            "token_id":        sig.token_id,
            "side":            sig.side,
            "entry":           entry,
            "fair_value":      sig.fair_value,
            "shares":          shares,
            "confidence":      sig.confidence,
            "close_after":     close_after,
            "momentum_signal": getattr(sig, "momentum_signal", 0.0),
            "imbalance_signal":getattr(sig, "imbalance_signal", 0.0),
            "rel_strength":    getattr(sig, "rel_strength", 0.0),
        })
        self._save_sim_queue()

    def _process_sim_queue(self) -> None:
        now = time.time()
        still_open = []
        try:
            for sim in self._sim_queue:
                if now < sim["close_after"]:
                    still_open.append(sim)
                    continue

                exit_price = None

                if sim.get("market_id"):
                    try:
                        mkt = self._client.get_clob_market(sim["market_id"])
                        if mkt:
                            for tok in mkt.get("tokens") or []:
                                if str(tok.get("token_id", "")) == sim["token_id"]:
                                    _raw_p = tok.get("price")
                                    if _raw_p is not None:
                                        p = float(_raw_p)
                                        # p==0.0 is a valid resolved-loss price
                                        if p > 0.95 or p < 0.05:
                                            exit_price = p
                                    break
                    except Exception:
                        pass

                if exit_price is None:
                    try:
                        ob = self._client.get_order_book(sim["token_id"])
                        if ob:
                            if ob.best_bid > 0.95:
                                exit_price = ob.best_bid
                            elif 0.0 < ob.best_bid < 0.05:
                                exit_price = ob.best_bid
                    except Exception:
                        pass

                if exit_price is None and now < sim["close_after"] + 600:
                    still_open.append(sim)
                    continue

                if exit_price is None:
                    try:
                        ob = self._client.get_order_book(sim["token_id"])
                        if ob and ob.mid > 0:
                            # Use the live mid price — accurate regardless of resolution state
                            exit_price = ob.mid
                    except Exception:
                        pass
                if exit_price is None:
                    # True last resort: no orderbook data available. Use entry price
                    # (breakeven) rather than fair_value which is always > entry and
                    # would produce a synthetic win every time.
                    exit_price = sim["entry"]

                # Guard: skip if already closed (e.g., via EARLY-EXIT in monitoring)
                if sim["token_id"] not in self._sim._open:
                    logger.debug(
                        f"[SIM-QUEUE] {sim['token_id'][:12]}… already closed — "
                        "skipping phantom double-close"
                    )
                    continue

                self._sim.close_position(sim["token_id"], exit_price)

                gross_pnl  = round(sim["shares"] * (exit_price - sim["entry"]), 4)
                entry_usdc = sim["shares"] * sim["entry"]
                exit_usdc  = sim["shares"] * exit_price
                fee        = self._risk.trade_fee(entry_usdc, exit_usdc)
                net_pnl    = round(gross_pnl - fee, 4)

                if net_pnl >= 0:
                    self._dash_state.add_exec_log("filled",
                        f"FILLED +${net_pnl:.2f} (fee ${fee:.3f}) // market converged  \"{sim['question'][:38]}\"")
                else:
                    self._dash_state.add_exec_log("slipped",
                        f"SLIPPED ${net_pnl:.2f} (fee ${fee:.3f}) // adverse fill  \"{sim['question'][:38]}\"")

                _sim_tid = sim["token_id"]
                # In DRY_RUN, write sim journal entry if not already tracked.
                # In live mode, the real trade already recorded itself — skip to avoid
                # polluting the journal with dry_run=True phantom entries.
                if config.DRY_RUN and not self._learner.has_open(token_id=_sim_tid):
                    self._learner.record_open(
                        market_id=sim.get("market_id", ""),
                        token_id=_sim_tid,
                        side=sim.get("side", "YES"),
                        question=sim.get("question", ""),
                        entry_price=sim.get("entry", 0.5),
                        shares=sim.get("shares", 0.0),
                        cost_usdc=sim.get("shares", 0.0) * sim.get("entry", 0.5),
                        momentum_signal=sim.get("momentum_signal", 0.0),
                        imbalance_signal=sim.get("imbalance_signal", 0.0),
                        composite_signal=0.0,
                        confidence=sim.get("confidence", "LOW"),
                        rel_strength=sim.get("rel_strength", 0.0),
                        dry_run=True,
                    )
                sim_cost = sim.get("shares", 0.0) * sim.get("entry", 0.5)
                if config.DRY_RUN:
                    self._learner.record_close(_sim_tid, exit_price)
                    # Only update risk accounting from sim in DRY_RUN — in live mode,
                    # _close_position() already recorded the real PnL via record_close().
                    # Calling it again here would double-count daily P&L.
                    self._risk.record_close(pnl_usdc=net_pnl, cost_usdc=sim_cost)
                self._dash_state.record_closed_trade(gross_pnl, fee_usdc=fee)

                self._positions.pop(sim["token_id"], None)

        finally:
            # Always commit whatever we've processed — even if an exception fires
            # mid-loop. Without this, queue items that already ran could re-process
            # on the next tick, double-counting P&L.
            self._sim_queue = still_open
            self._save_sim_queue()

        if config.DRY_RUN:
            sim_open_cost = sum(t.cost_usdc for t in self._sim._open.values())
            sim_equity = self._sim._wallet + sim_open_cost
            if sim_equity > 0:
                self._risk.set_wallet_balance(sim_equity)

    def _save_sim_queue(self) -> None:
        try:
            self._sim_queue_path.parent.mkdir(parents=True, exist_ok=True)
            self._sim_queue_path.write_text(json.dumps(self._sim_queue, indent=2))
        except Exception as exc:
            logger.warning(f"_save_sim_queue failed: {exc}")

    def _load_sim_queue(self) -> list[dict]:
        if not self._sim_queue_path.exists():
            return []
        try:
            entries = json.loads(self._sim_queue_path.read_text())
            if entries:
                logger.info(f"Restored {len(entries)} pending sim trades from disk")
            return entries
        except Exception as exc:
            logger.warning(f"_load_sim_queue failed: {exc}")
            return []

    def _check_sim_loss_limit(self, sim_equity: float) -> None:
        from src.sim import STARTING_BALANCE as SIM_START
        from src.utils import alerter, atomic_json_write
        loss_limit = SIM_START * self._SIM_LOSS_RESET_PCT

        needs_reset = (
            sim_equity < self._SIM_MIN_TRADEABLE
            or sim_equity < loss_limit
        )
        if not needs_reset:
            return

        reason = (
            f"below minimum trade size (${sim_equity:.2f} < ${self._SIM_MIN_TRADEABLE:.2f})"
            if sim_equity < self._SIM_MIN_TRADEABLE
            else f"loss limit hit (${sim_equity:.2f} < {self._SIM_LOSS_RESET_PCT:.0%} of ${SIM_START:.0f})"
        )
        summary = self._sim.reset_wallet()

        wr_str = f"{summary['win_rate']:.1%}" if summary['total_trades'] else "—"
        alerter.send(
            f"SIM wallet reset — {reason}\n"
            f"Session: {summary['total_trades']}T  WR {wr_str}  "
            f"P&L {summary['total_pnl']:+.2f}  "
            f"→ Restarting at ${summary['new_wallet']:.0f}",
            level="warning",
        )

        fail_log_path = Path("data/sim_failures.json")
        try:
            failures = json.loads(fail_log_path.read_text()) if fail_log_path.exists() else []
        except Exception:
            failures = []
        failures.append({**summary, "reason": reason,
                         "learned": self._dash_state.learned or {}})
        atomic_json_write(fail_log_path, failures)
        logger.info(f"[SIM] Failure logged to {fail_log_path}")

    def _reset_sim_wallet(self) -> str:
        from src.sim import STARTING_BALANCE as SIM_START
        from src.utils import alerter
        summary = self._sim.reset_wallet(SIM_START)
        wr_str = f"{summary['win_rate']:.1%}" if summary['total_trades'] else "—"
        msg = (
            f"SIM wallet manually reset to ${SIM_START:.0f}\n"
            f"Ended session: {summary['total_trades']}T  WR {wr_str}  "
            f"P&L {summary['total_pnl']:+.2f}"
        )
        alerter.send(msg, level="info")
        return msg
