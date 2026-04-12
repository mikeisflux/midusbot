"""
Position monitoring — manage open positions each loop iteration.

MonitoringMixin provides:
  _manage_positions     — check all open positions; trigger exits as needed
  _gamma_position_price — fetch resolved token price from CLOB market data
"""
from __future__ import annotations

import threading
import time

from loguru import logger

from src.strategy import _detect_updown_market, _updown_window_mins
import config


class MonitoringMixin:

    def _start_news_arb_fast_monitor(self) -> None:
        """
        Background thread: checks news_arb/price_velocity positions every 2 seconds
        and fires scalp exits the moment target/stop is hit — does NOT wait for the
        15-second main loop. Call once after bot startup.
        """
        if getattr(self, "_news_monitor_started", False):
            return
        self._news_monitor_started = True
        self._news_monitor_lock = threading.Lock()

        def _loop() -> None:
            while getattr(self, "_running", True):
                try:
                    self._fast_news_scalp_check()
                except Exception as exc:
                    logger.debug(f"[NEWS-FAST-MON] error: {exc}")
                time.sleep(2)

        t = threading.Thread(target=_loop, daemon=True, name="news-arb-monitor")
        t.start()
        logger.info("[NEWS-FAST-MON] Fast position monitor started (2s interval)")

    def _fast_news_scalp_check(self) -> None:
        """Check news_arb positions right now and exit if target/stop hit."""
        news_positions = {
            tid: pos for tid, pos in list(self._positions.items())
            if getattr(pos, "strategy", "") in ("news_arb", "price_velocity")
        }
        if not news_positions:
            return

        scalp_target = float(getattr(config, "NEWS_ARB_SCALP_TARGET", 0.04))
        scalp_stop   = float(getattr(config, "NEWS_ARB_SCALP_STOP",   0.03))

        for token_id, pos in news_positions.items():
            try:
                ob = self._client.get_order_book(token_id)
                if ob is None or ob.mid <= 0:
                    continue
                current_price = ob.mid
                move = current_price - pos.entry_price

                if move >= scalp_target:
                    logger.info(
                        f"[NEWS-FAST] TAKE PROFIT {pos.side} "
                        f"{pos.entry_price:.3f}→{current_price:.3f} (+{move:.3f}) "
                        f"{pos.question[:55]}"
                    )
                    with self._news_monitor_lock:
                        if token_id in self._positions:
                            self._close_position(token_id, current_price=current_price)

                elif move <= -scalp_stop:
                    logger.warning(
                        f"[NEWS-FAST] STOP LOSS {pos.side} "
                        f"{pos.entry_price:.3f}→{current_price:.3f} ({move:.3f}) "
                        f"{pos.question[:55]}"
                    )
                    with self._news_monitor_lock:
                        if token_id in self._positions:
                            self._close_position(token_id, current_price=current_price)

            except Exception as exc:
                logger.debug(f"[NEWS-FAST] {token_id[:12]} check failed: {exc}")

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
                    if not config.DRY_RUN and _final_price >= 0.90:
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

            # ── Empty-book stop for news_arb ──────────────────────────────────
            # If the orderbook is completely empty and we can't price the position,
            # we have no way to stop-loss normally. For news_arb positions this is
            # almost always bad: the market has resolved or gone one-sided against us.
            # Force-close at whatever we can get rather than hold a worthless bag.
            if current_price is None and getattr(pos, "strategy", "") in ("news_arb", "price_velocity"):
                logger.warning(
                    f"[NEWS-SCALP] Empty book — no price for {pos.side} {pos.question[:50]} "
                    f"entry={pos.entry_price:.3f} — force-closing to avoid holding $0 bag"
                )
                to_close.append((token_id, 0.01))
                continue

            # ── BTC penny bets / opposing-side bets: bypass stop-loss and auto-claim ──
            # btc_penny_hedge positions always resolve to 1.0 — never sell via CLOB.
            # Attempting a CLOB sell after settlement fails (balance=0) because
            # Polymarket auto-redeems the winning token before we can sell it.
            # The reconcile loop / ghost-pos handler records P&L on settlement.
            _is_penny_strat = getattr(pos, "strategy", "") in ("btc_penny", "btc_penny_hedge")
            if _is_penny_strat and (config.DRY_RUN or current_price > 0.03):
                position_snapshots.append((pos, current_price))
                continue
            # current_price <= 0.03 → fall through to LOSS handling below

            # ── Chainlink-confirmed positions: bypass stop-loss ───────────────
            # Oracle has already confirmed the outcome — pre-settlement price
            # dips are noise. Stop-loss here books a loss on a guaranteed winner.
            # Hold to resolution; auto-claim / ghost-pos handler records P&L.
            if getattr(pos, "strategy", "") == "chainlink":
                position_snapshots.append((pos, current_price))
                continue

            # ── WIN auto-claim ────────────────────────────────────────────────
            if not config.DRY_RUN:
                if current_price >= 0.90:
                    logger.info(
                        f"AUTO-CLAIM: WIN @ {current_price:.3f} "
                        f"({pos.shares:.2f} shares) — selling at market price  {pos.question[:50]}"
                    )
                    # Sell at current market price first — faster and avoids
                    # "result for condition not received yet" if market hasn't
                    # fully resolved. _close_position falls back to redeem only
                    # if the orderbook is gone (market already settled).
                    self._close_position(token_id, current_price=current_price)
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
                if (pos.entry_price and current_price is not None) else 0.0
            )

            position_snapshots.append((pos, current_price))

            # ── Complete dual-arb pairs: hold to resolution ───────────────────
            # YES and NO legs of the same arb trade always pay $1.00 combined at
            # resolution. Applying any directional exit (stop-loss, trailing stop,
            # early-exit) to one leg DESTROYS the hedge and turns a guaranteed
            # profit into a naked directional bet (confirmed XRP bug, 2026-04-12).
            #
            # Rule: if pair_id is set and the partner leg still exists → hold both
            # until auto-claim (≥0.90) or auto-clear (≤0.03) fires naturally.
            #
            # Unpaired legs (YES bought but NO fill failed) get a timeout: close
            # after ARB_MAX_UNPAIRED_SECS (default 30 min) to free capital.
            if getattr(pos, "strategy", "") == "arb" and getattr(pos, "pair_id", ""):
                _partner_tid = getattr(pos, "arb_partner_token_id", "")
                if _partner_tid and _partner_tid in self._positions:
                    # Complete pair — skip ALL directional exits
                    continue
                # Unpaired leg: enforce holding time limit
                _max_unpaired = int(getattr(config, "ARB_MAX_UNPAIRED_SECS", 1800))
                _held = time.time() - getattr(pos, "entry_time", 0)
                if _held > _max_unpaired:
                    to_close.append((token_id, current_price))
                    logger.warning(
                        f"[ARB-TIMEOUT] Unpaired {pos.side} held {_held:.0f}s "
                        f"(max {_max_unpaired}s) — closing to free capital  "
                        f"{pos.question[:45]}"
                    )
                    continue

            # ── News arb scalp exits (strategy="news_arb") ───────────────────
            # We're trading the repricing wave, not holding to resolution.
            # News breaks → market is mispriced → MMs reprice over next few minutes.
            # Exit as soon as we've captured enough of that move. Win/loss of the
            # underlying prediction is irrelevant — we've already banked the spread.
            #
            # Also applies to externally-reconciled positions that are not UpDown
            # markets — these are news_arb trades from a previous session that
            # were reloaded without the strategy tag.
            _is_news_arb_pos = getattr(pos, "strategy", "") in ("news_arb", "price_velocity")
            _is_ext_event = pos.is_external and _detect_updown_market(pos.question) is None
            if (_is_news_arb_pos or _is_ext_event) and current_price is not None:
                _scalp_target = float(getattr(config, "NEWS_ARB_SCALP_TARGET", 0.07))
                _scalp_stop   = float(getattr(config, "NEWS_ARB_SCALP_STOP",   0.05))
                _price_move   = current_price - pos.entry_price

                if _price_move >= _scalp_target:
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[NEWS-SCALP] Taking profit: {pos.side} {pos.entry_price:.3f}"
                        f"→{current_price:.3f} (+{_price_move:.3f}) "
                        f"target={_scalp_target:.2f}  {pos.question[:50]}"
                    )
                    continue

                if _price_move <= -_scalp_stop:
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[NEWS-SCALP] Stop loss: {pos.side} {pos.entry_price:.3f}"
                        f"→{current_price:.3f} ({_price_move:.3f}) "
                        f"stop={_scalp_stop:.2f}  {pos.question[:50]}"
                    )
                    continue

            # ── Hard stop-loss: cut any position that lost ≥35% ──────────────
            if pos.entry_price > 0 and pnl_pct <= -0.35:
                logger.warning(
                    f"[STOP-40%] {pos.side} {pos.question[:45]}  "
                    f"entry={pos.entry_price:.3f} → now={current_price:.3f}  "
                    f"loss={pnl_pct:.1%} — cutting loss to protect capital"
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
            # current_price is always the token price for the SIDE held.
            # YES token falls when losing; NO token falls when losing.
            # Both sides use the same direction: low price = loss, high price = gain.
            if current_price is not None:
                if current_price < config.EARLY_EXIT_LOSS_THRESHOLD:
                    # Cut immediately — losers resolve to 0.00 at window close.
                    # Time gate removed: holding until midpoint was letting losers
                    # bleed to near-zero (data shows -93% to -100% losses on held positions).
                    _elapsed_s = time.time() - getattr(pos, "entry_time", 0)
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[EARLY-EXIT] Cutting losing {pos.side} position at {current_price:.3f} "
                        f"({pnl_pct:+.1%}) {_elapsed_s:.0f}s in — {pos.question[:40]}"
                    )
                    continue

                # ── Update high-water mark ────────────────────────────────────
                if current_price > pos.high_water_mark:
                    pos.high_water_mark = current_price

                # ── Trailing stop: lock in 65% of peak gains ──────────────────
                # Activates once we've gained at least 8 cents from entry.
                # Trigger: price retreats below 65% of the peak gain from entry.
                _peak_gain = pos.high_water_mark - pos.entry_price
                if _peak_gain >= 0.08:
                    _trail_floor = pos.entry_price + _peak_gain * 0.65
                    if current_price < _trail_floor:
                        to_close.append((token_id, current_price))
                        logger.info(
                            f"[TRAIL-STOP] {pos.side} retreated from peak {pos.high_water_mark:.3f} "
                            f"to {current_price:.3f} — trail floor={_trail_floor:.3f} "
                            f"({pnl_pct:+.1%}) — {pos.question[:40]}"
                        )
                        continue

                # Winners go to 1.00 and are auto-claimed at ≥0.90. Only exit
                # early if price has retreated significantly from a high peak
                # (handled by trailing stop above) or hit the gain threshold.
                if current_price > config.EARLY_EXIT_GAIN_THRESHOLD:
                    to_close.append((token_id, current_price))
                    logger.info(
                        f"[EARLY-EXIT] Locking in {pos.side} gain at {current_price:.3f} "
                        f"({pnl_pct:+.1%}) — {pos.question[:40]}"
                    )
                    continue

            # ── Time-based exit: sell before window resolves ──────────────────
            # 5-min markets snap to 0 at close — don't hold into resolution if not winning.
            if not pos.is_external and current_price is not None:
                _sym = _detect_updown_market(pos.question)
                if _sym and getattr(pos, "entry_time", 0) > 0:
                    _elapsed = time.time() - pos.entry_time
                    _window_secs = (_updown_window_mins(pos.question) or 5) * 60
                    _secs_remaining = _window_secs - _elapsed
                    if _secs_remaining < 75 and current_price < 0.62:
                        to_close.append((token_id, current_price))
                        logger.info(
                            f"[TIME-EXIT] {_secs_remaining:.0f}s left, token={current_price:.3f} "
                            f"— exiting before resolution  {pos.question[:40]}"
                        )
                        continue

            if pos.entry_price > 0:
                if self._risk.should_stop_loss(pnl_pct):
                    logger.warning(f"STOP-LOSS {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))
                elif self._risk.should_take_profit(pnl_pct):
                    logger.info(f"TAKE-PROFIT {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))

        self._dash_state.positions = position_snapshots

        for token_id, cur_price in to_close:
            self._close_position(token_id, current_price=cur_price)

    def _gamma_position_price(self, pos) -> float | None:
        """Fetch the current token price from CLOB market data (fallback for resolved markets).
        Returns None if price cannot be determined — callers must handle this."""
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
        # Do NOT fall back to entry_price — that masks losses and disables stop-loss.
        return None
