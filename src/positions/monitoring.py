"""
Position monitoring — manage open positions each loop iteration.

MonitoringMixin provides:
  _manage_positions     — check all open positions; trigger exits as needed
  _gamma_position_price — fetch resolved token price from CLOB market data
"""
from __future__ import annotations

from loguru import logger

from src.strategy import _detect_updown_market
import config


class MonitoringMixin:

    def _manage_positions(self) -> None:
        if not self._positions:
            self._dash_state.positions = []
            return

        to_close: list[tuple[str, float]] = []
        position_snapshots: list[tuple] = []

        for token_id, pos in list(self._positions.items()):
            ob = self._client.get_order_book(token_id)
            book_is_empty = ob is None or (ob.best_bid == 0.0 and ob.best_ask == 1.0)

            clob_mkt = None
            if book_is_empty and pos.market_id:
                try:
                    clob_mkt = self._client.get_clob_market(pos.market_id)
                except Exception:
                    pass

            if clob_mkt:
                q = clob_mkt.get("question", "")
                if q and (pos.question.startswith("[token:") or len(pos.question) <= 20):
                    pos.question = q

            # ── Closed/external market handling ───────────────────────────────
            if ob is None and pos.is_external and clob_mkt is not None:
                market_active = clob_mkt.get("active", True)
                market_closed = clob_mkt.get("closed", False)
                if not market_active or market_closed:
                    _final_price = self._gamma_position_price(pos)
                    _risk_closed = False
                    if not config.DRY_RUN and _final_price >= 0.97:
                        logger.info(
                            f"AUTO-CLAIM (external): resolved WIN @ {_final_price:.3f} "
                            f"({pos.shares:.2f} shares)  {pos.question[:50]}"
                        )
                        try:
                            neg_risk = bool(clob_mkt.get("neg_risk", False))
                            redeemed = self._client.redeem_position(pos.market_id, neg_risk=neg_risk)
                            if redeemed:
                                pnl = self._learner.record_close(token_id, _final_price)
                                cost = pos.cost_usdc if not pos.is_external else 0.0
                                fee  = self._risk.trade_fee(cost, _final_price * pos.shares)
                                self._risk.record_close(pnl_usdc=pnl - fee, cost_usdc=cost)
                                _risk_closed = True
                                self._dash_state.record_closed_trade(pnl, fee_usdc=fee)
                                symbol = _detect_updown_market(pos.question)
                                if symbol:
                                    direction_bet = "UP" if pos.side == "YES" else "DOWN"
                                    self._trend_tracker.record_result(symbol, direction_bet, won=True)
                                logger.info(f"CLAIMED (external): {pos.question[:50]}  P&L=${pnl:+.2f}")
                        except Exception as _e:
                            logger.warning(f"AUTO-CLAIM (external) failed: {_e}")
                    else:
                        logger.info(
                            f"PRUNED (LOSS): market closed — auto-redeeming $0 "
                            f"{pos.question[:55]}"
                        )
                        if not config.DRY_RUN and pos.market_id:
                            try:
                                neg_risk = bool(clob_mkt.get("neg_risk", False))
                                self._client.redeem_position(pos.market_id, neg_risk=neg_risk)
                                if self._redeemed_tokens is not None:
                                    self._redeemed_tokens.add(token_id)
                                    from src.positions.store import save_redeemed
                                    save_redeemed(self._redeemed_tokens)
                            except Exception as _re:
                                logger.debug(f"Redeem $0 loss failed (ok to ignore): {_re}")
                        self._learner.record_close(token_id, 0.0)
                        self._dash_state.record_closed_trade(0.0, fee_usdc=0.0)
                        _prune_sym = _detect_updown_market(pos.question)
                        if _prune_sym:
                            _prune_dir = "UP" if pos.side == "YES" else "DOWN"
                            self._trend_tracker.record_result(_prune_sym, _prune_dir, won=False)
                    if not _risk_closed:
                        _prune_cost = pos.cost_usdc if not pos.is_external else 0.0
                        self._risk.record_close(pnl_usdc=-_prune_cost, cost_usdc=_prune_cost)
                    if pos.market_id:
                        self._mark_market_closed(pos.market_id)
                    del self._positions[token_id]
                    self._save_positions()
                    continue

            # ── Price lookup ──────────────────────────────────────────────────
            if book_is_empty:
                current_price = self._gamma_position_price(pos)
            else:
                current_price = ob.mid

            # ── WIN auto-claim ────────────────────────────────────────────────
            if not config.DRY_RUN:
                if current_price >= 0.97:
                    logger.info(
                        f"AUTO-CLAIM: resolved WIN @ {current_price:.3f} "
                        f"({pos.shares:.2f} shares)  {pos.question[:50]}"
                    )
                    try:
                        mkt = self._client.get_clob_market(pos.market_id) if pos.market_id else None
                        neg_risk = bool(mkt.get("neg_risk", False)) if mkt else False
                    except Exception:
                        neg_risk = False
                    redeemed = self._client.redeem_position(pos.market_id, neg_risk=neg_risk)
                    if redeemed:
                        exit_price = current_price
                        exit_usdc  = exit_price * pos.shares
                        pnl = self._learner.record_close(token_id, exit_price)
                        cost = pos.cost_usdc if not pos.is_external else 0.0
                        fee  = self._risk.trade_fee(cost, exit_usdc)
                        self._risk.record_close(pnl_usdc=pnl - fee, cost_usdc=cost)
                        self._dash_state.record_closed_trade(pnl, fee_usdc=fee)
                        symbol = _detect_updown_market(pos.question)
                        if symbol:
                            direction_bet = "UP" if pos.side == "YES" else "DOWN"
                            self._trend_tracker.record_result(symbol, direction_bet, won=True)
                        if pos.market_id:
                            self._mark_market_closed(pos.market_id)
                        del self._positions[token_id]
                        self._save_positions()
                        logger.info(
                            f"CLAIMED: {pos.side} {pos.question[:40]}  "
                            f"P&L=${pnl:+.2f}  net=${pnl - fee:+.2f}"
                        )
                    else:
                        self._close_position(token_id, current_price=current_price, manual=True)
                    continue

                if current_price <= 0.03:
                    pnl = self._learner.record_close(token_id, current_price)
                    cost = pos.cost_usdc if not pos.is_external else 0.0
                    self._risk.record_close(pnl_usdc=pnl, cost_usdc=cost)
                    self._dash_state.record_closed_trade(pnl, fee_usdc=0.0)
                    symbol = _detect_updown_market(pos.question)
                    if symbol:
                        direction_bet = "UP" if pos.side == "YES" else "DOWN"
                        self._trend_tracker.record_result(symbol, direction_bet, won=False)
                    del self._positions[token_id]
                    self._save_positions()
                    logger.info(f"AUTO-CLEAR: resolved NO — position removed  {pos.question[:50]}")
                    continue

            pnl_pct = (
                (current_price - pos.entry_price) / pos.entry_price
                if pos.entry_price else 0.0
            )

            position_snapshots.append((pos, current_price))

            # ── Hard stop-loss: cut any position that lost ≥40% ──────────────
            if pos.entry_price > 0 and pnl_pct <= -0.40:
                logger.warning(
                    f"[STOP-40%] {pos.side} {pos.question[:45]}  "
                    f"entry={pos.entry_price:.3f} → now={current_price:.3f}  "
                    f"loss={pnl_pct:.1%} — selling to recover remaining capital"
                )
                _stop_symbol = _detect_updown_market(pos.question)
                if _stop_symbol:
                    _stop_dir = "UP" if pos.side == "YES" else "DOWN"
                    self._trend_tracker.record_result(_stop_symbol, _stop_dir, won=False)
                    logger.debug(
                        f"[STOP-40%] Recorded LOSS for {_stop_symbol} {_stop_dir} "
                        f"in trend tracker — streak reset"
                    )
                to_close.append((token_id, current_price))
                continue

            # ── Early exit / loss cut ─────────────────────────────────────────
            if current_price is not None:
                if pos.side == "YES" and current_price < config.EARLY_EXIT_LOSS_THRESHOLD:
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[EARLY-EXIT] Cutting losing YES position at {current_price:.3f} "
                        f"({pnl_pct:+.1%}) — {pos.question[:40]}"
                    )
                    continue
                if pos.side == "NO" and current_price > (1.0 - config.EARLY_EXIT_LOSS_THRESHOLD):
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[EARLY-EXIT] Cutting losing NO position at {current_price:.3f} "
                        f"({pnl_pct:+.1%}) — {pos.question[:40]}"
                    )
                    continue
                if pos.side == "YES" and current_price > config.EARLY_EXIT_GAIN_THRESHOLD:
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[EARLY-EXIT] Locking in YES gain at {current_price:.3f} "
                        f"({pnl_pct:+.1%}) — {pos.question[:40]}"
                    )
                    continue
                if pos.side == "NO" and current_price < (1.0 - config.EARLY_EXIT_GAIN_THRESHOLD):
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[EARLY-EXIT] Locking in NO gain at {current_price:.3f} "
                        f"({pnl_pct:+.1%}) — {pos.question[:40]}"
                    )
                    continue

            if not pos.is_external and pos.entry_price > 0:
                if self._risk.should_stop_loss(pnl_pct):
                    logger.warning(f"STOP-LOSS {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))
                elif self._risk.should_take_profit(pnl_pct):
                    logger.info(f"TAKE-PROFIT {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))

        self._dash_state.positions = position_snapshots

        for token_id, cur_price in to_close:
            self._close_position(token_id, current_price=cur_price)

    def _gamma_position_price(self, pos) -> float:
        """Fetch the current token price from CLOB market data (fallback for resolved markets)."""
        if pos.market_id:
            try:
                mkt = self._client.get_clob_market(pos.market_id)
                if mkt:
                    tokens = mkt.get("tokens") or []
                    for tok in tokens:
                        if str(tok.get("token_id", "")) == pos.token_id:
                            # Return 0 for resolved-loss tokens (price=0 is real).
                            raw = tok.get("price")
                            if raw is not None:
                                price = float(raw)
                                logger.debug(
                                    f"[CLOB fallback] {pos.side} price={price:.3f}  "
                                    f"{pos.question[:50]}"
                                )
                                return price
            except Exception as exc:
                logger.debug(f"_gamma_position_price CLOB fallback failed: {exc}")
        return pos.entry_price
