"""
Position management — mixin for PolymarketBot.
Handles open/close lifecycle, reconciliation, persistence, and wallet sync.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from loguru import logger

from src.strategy import _detect_updown_market, _updown_window_mins
import config

_POSITIONS_FILE      = Path("data/positions.json")
_REDEEMED_FILE       = Path("data/redeemed_tokens.json")
_CLOSED_MARKETS_FILE = Path("data/closed_market_ids.json")


def _load_closed_market_ids() -> set[str]:
    try:
        if _CLOSED_MARKETS_FILE.exists():
            return set(json.loads(_CLOSED_MARKETS_FILE.read_text()))
    except Exception:
        pass
    return set()


def _save_closed_market_ids(ids: set[str]) -> None:
    try:
        _CLOSED_MARKETS_FILE.write_text(json.dumps(sorted(ids), indent=2))
    except Exception as exc:
        logger.warning(f"Could not save closed_market_ids: {exc}")


def _load_redeemed() -> set[str]:
    """Load persisted set of already-redeemed token IDs."""
    try:
        if _REDEEMED_FILE.exists():
            return set(json.loads(_REDEEMED_FILE.read_text()))
    except Exception:
        pass
    return set()


def _save_redeemed(redeemed: set[str]) -> None:
    try:
        _REDEEMED_FILE.write_text(json.dumps(sorted(redeemed), indent=2))
    except Exception as exc:
        logger.warning(f"Could not save redeemed_tokens: {exc}")


class PositionsMixin:
    _DAILY_LOSS_ALERT_PCT: float = 0.10
    _daily_loss_alerted:   bool  = False
    _redeemed_tokens:      set   = None  # type: ignore[assignment]  — initialised lazily

    # ------------------------------------------------------------------
    # Position monitoring
    # ------------------------------------------------------------------

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

            if ob is None and pos.is_external and clob_mkt is not None:
                market_active = clob_mkt.get("active", True)
                market_closed = clob_mkt.get("closed", False)
                if not market_active or market_closed:
                    # Check price before pruning — may be a winning position to redeem
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
                        # Redeem even for $0 — clears position from Polymarket portfolio
                        # so user doesn't see stale "Redeem" buttons everywhere.
                        if not config.DRY_RUN and pos.market_id:
                            try:
                                neg_risk = bool(clob_mkt.get("neg_risk", False))
                                self._client.redeem_position(pos.market_id, neg_risk=neg_risk)
                                if self._redeemed_tokens is not None:
                                    self._redeemed_tokens.add(token_id)
                                    _save_redeemed(self._redeemed_tokens)
                            except Exception as _re:
                                logger.debug(f"Redeem $0 loss failed (ok to ignore): {_re}")
                        # Record the loss so learner + trend tracker stay calibrated.
                        self._learner.record_close(token_id, 0.0)
                        self._dash_state.record_closed_trade(0.0, fee_usdc=0.0)
                        _prune_sym = _detect_updown_market(pos.question)
                        if _prune_sym:
                            _prune_dir = "UP" if pos.side == "YES" else "DOWN"
                            self._trend_tracker.record_result(_prune_sym, _prune_dir, won=False)
                    # Free exposure — skip if WIN path already did it
                    if not _risk_closed:
                        _prune_cost = pos.cost_usdc if not pos.is_external else 0.0
                        self._risk.record_close(pnl_usdc=-_prune_cost, cost_usdc=_prune_cost)
                    if pos.market_id:
                        self._mark_market_closed(pos.market_id)
                    del self._positions[token_id]
                    self._save_positions()
                    continue

            if book_is_empty:
                current_price = self._gamma_position_price(pos)
            else:
                current_price = ob.mid

            if not config.DRY_RUN:
                if current_price >= 0.97:
                    logger.info(
                        f"AUTO-CLAIM: resolved WIN @ {current_price:.3f} "
                        f"({pos.shares:.2f} shares)  {pos.question[:50]}"
                    )
                    # Go straight to redeem — no order book on resolved markets
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
                        # Redeem failed — fall back to close via order
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

            # Hard stop-loss: cut any position that has lost ≥40% of its entry
            # value regardless of side or whether the position is external.
            # Recovers remaining capital (60¢ on the dollar) rather than riding
            # to near-zero. Applies even when the adaptive stop-loss is looser.
            if pos.entry_price > 0 and pnl_pct <= -0.40:
                logger.warning(
                    f"[STOP-40%] {pos.side} {pos.question[:45]}  "
                    f"entry={pos.entry_price:.3f} → now={current_price:.3f}  "
                    f"loss={pnl_pct:.1%} — selling to recover remaining capital"
                )
                # ── Learning: record this as a wrong-direction bet ────────────
                # _close_position (called below) handles: learner journal P&L,
                # risk.record_close (updates _daily_pnl + frees exposure), and
                # dash_state — so the wallet/daily-loss counter stays correct.
                #
                # What _close_position does NOT do for mid-price exits: update
                # the trend tracker (it only fires at price ≥0.95 or ≤0.05).
                # Record the loss here so the streak direction resets — prevents
                # the bot from doubling down on a direction that just lost 40%.
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

            # Early exit / loss cut — heuristic exits based on current price extremes.
            # Exits early to redeploy capital or lock in gains before potential revert.
            # Apply to ALL positions (including those loaded from disk / reconciled externals)
            # so that no position ever rides to zero without an attempted sell.
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
        if pos.market_id:
            try:
                mkt = self._client.get_clob_market(pos.market_id)
                if mkt:
                    tokens = mkt.get("tokens") or []
                    for tok in tokens:
                        if str(tok.get("token_id", "")) == pos.token_id:
                            # Return 0 for resolved-loss tokens (price=0 is real).
                            # Old code skipped 0 → fell back to entry_price → masked losses.
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

    def _close_position(self, token_id: str, current_price: float | None = None, manual: bool = False) -> bool:
        pos = self._positions.get(token_id)
        if not pos:
            logger.warning(f"_close_position: token_id {token_id[:12]}… not found in open positions")
            return False

        resp = None

        if config.SWAPS_API_KEY:
            tick_size = "0.01"
            neg_risk  = False
            if pos.market_id:
                try:
                    mkt = self._client.get_clob_market(pos.market_id)
                    if mkt:
                        tick_size = str(mkt.get("minimum_tick_size", "0.01"))
                        neg_risk  = bool(mkt.get("neg_risk", False))
                        if pos.question.startswith("[token:") or len(pos.question) < 25:
                            q = mkt.get("question", "")
                            if q:
                                pos.question = q
                except Exception as exc:
                    logger.debug(f"get_clob_market failed, using defaults: {exc}")
            resp = self._client.sell_via_swaps(token_id, pos.shares, tick_size, neg_risk)
            if resp and not resp.get("dry_run"):
                ok = resp.get("orderResponse", {}).get("success", False)
                if not ok:
                    logger.warning(f"swaps.xyz sell failed: {resp} — falling back to CLOB")
                    resp = None

        if resp is None:
            ob = self._client.get_order_book(token_id)
            book_best_bid = ob.best_bid if ob else 0.0
            if book_best_bid > 0.0:
                sell_price = book_best_bid
            elif current_price is not None and current_price > 0.05:
                sell_price = current_price
            else:
                sell_price = pos.entry_price
            # 5-min UpDown markets: use FOK so the sell either fills immediately
            # or cancels. A GTC order sits on the book until market resolution,
            # at which point it's cancelled unfilled — zero recovery. FOK guarantees
            # we take whatever bid is available right now.
            _is_updown = _detect_updown_market(pos.question) is not None
            resp = self._client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price=sell_price,
                size=pos.shares,
                fok=_is_updown,
            )

        if resp is None and pos.market_id and current_price is not None and current_price >= 0.97:
            neg_risk = False
            try:
                mkt = self._client.get_clob_market(pos.market_id)
                if mkt:
                    neg_risk = bool(mkt.get("neg_risk", False))
            except Exception:
                pass
            redeemed = self._client.redeem_position(pos.market_id, neg_risk=neg_risk)
            if redeemed:
                resp = {"redeemed": True}

        if resp:
            exit_price = current_price or pos.entry_price
            exit_usdc  = exit_price * pos.shares
            fee  = self._risk.trade_fee(pos.cost_usdc, exit_usdc)
            pnl  = self._learner.record_close(token_id, exit_price)
            self._risk.record_close(pnl_usdc=pnl - fee, cost_usdc=pos.cost_usdc)
            self._dash_state.record_closed_trade(pnl, fee_usdc=fee)

            symbol = _detect_updown_market(pos.question)
            if symbol:
                direction_bet = "UP" if pos.side == "YES" else "DOWN"
                if exit_price >= 0.95:
                    self._trend_tracker.record_result(symbol, direction_bet, won=True)
                elif exit_price <= 0.05:
                    self._trend_tracker.record_result(symbol, direction_bet, won=False)

            if pos.market_id:
                self._mark_market_closed(pos.market_id)
            del self._positions[token_id]
            self._save_positions()
            logger.info(
                f"{'[SIM] ' if config.DRY_RUN else ''}Closed: {pos.side} {pos.question[:40]}  "
                f"P&L=${pnl:+.2f}  fee=${fee:.4f}  net=${pnl-fee:+.2f}"
            )

            # Log outcome to historical database for offline analysis (A5)
            try:
                import json as _json
                _outcome_log = Path("data/polymarket_outcomes.jsonl")
                _outcome_log.parent.mkdir(parents=True, exist_ok=True)
                with _outcome_log.open("a") as _fh:
                    _fh.write(_json.dumps({
                        "ts": time.time(),
                        "market_id": pos.market_id,
                        "question": pos.question,
                        "token_id": token_id,
                        "side": pos.side,
                        "entry_price": pos.entry_price,
                        "exit_price": exit_price,
                        "shares": pos.shares,
                        "pnl_usdc": exit_usdc - pos.cost_usdc,
                        "dry_run": config.DRY_RUN,
                    }) + "\n")
            except Exception:
                pass

            # Adverse selection tracking log (A6)
            try:
                import json as _json
                _adv_log = Path("data/adverse_selection.jsonl")
                _adv_log.parent.mkdir(parents=True, exist_ok=True)
                with _adv_log.open("a") as _fh:
                    _fh.write(_json.dumps({
                        "ts": time.time(),
                        "question": pos.question[:60],
                        "pnl_usdc": exit_usdc - pos.cost_usdc,
                    }) + "\n")
            except Exception:
                pass

            return True

        if manual:
            self._risk.record_close(cost_usdc=pos.cost_usdc)
            del self._positions[token_id]
            self._save_positions()
            logger.info(
                f"MANUAL-REMOVE: sell order unavailable (market closed?) — "
                f"removed from tracking: {pos.question[:55]}"
            )
            return True

        return False

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute_signal(self, sig) -> bool:
        _exec_start = time.time()
        if config.TRADING_PAUSED:
            return False

        # Cold-start warmup — block trading for 90s after launch while price
        # history accumulates; signals on stale/empty data are unreliable
        _warmup = getattr(self, "_warmup_until", 0.0)
        if time.time() < _warmup:
            _secs_left = int(_warmup - time.time())
            logger.debug(f"[WARMUP] Skipping signal — price feed warming up ({_secs_left}s remaining)")
            return False

        # Capital floor — hard stop if wallet is dangerously low
        _wallet = self._dash_state.wallet_balance or 0.0
        _floor  = float(getattr(config, "CAPITAL_FLOOR_USDC", 15.0))
        if _wallet > 0 and _wallet < _floor:
            logger.warning(
                f"[CAPITAL FLOOR] Wallet ${_wallet:.2f} below floor ${_floor:.2f} — "
                f"all trading halted. Replenish USDC to resume."
            )
            return False

        if sig.token_id in self._positions:
            logger.debug(f"Already tracking token {sig.token_id[:16]}… — skipping duplicate signal")
            return False

        sig_symbol = _detect_updown_market(sig.question)
        sig_window = _updown_window_mins(sig.question) if sig_symbol else None
        if sig_symbol and sig_window:
            for pos in self._positions.values():
                pos_symbol = _detect_updown_market(pos.question)
                pos_window = _updown_window_mins(pos.question) if pos_symbol else None
                if pos_symbol == sig_symbol and pos_window == sig_window:
                    logger.debug(
                        f"Skipping {sig_symbol} {sig_window}min UpDown — "
                        f"already open: {pos.question[:55]}"
                    )
                    return False

        usdc = self._risk.position_size(sig)
        if usdc <= 0:
            return False

        if sig.hours_to_close is not None:
            h = sig.hours_to_close
            if h <= 1:
                boost = 2.5 - (h / 1) * 0.5
            elif h <= 24:
                boost = 2.0 - ((h - 1) / 23) * 0.5
            elif h <= 48:
                boost = 1.5 - ((h - 24) / 24) * 0.25
            else:
                boost = 1.0
            if boost > 1.0:
                usdc = min(usdc * boost, config.MAX_POSITION_USDC)
                logger.debug(f"Urgency boost {boost:.2f}× — {h:.1f}h to close")

        limit_price = round(min(sig.fair_value, sig.market_price * 1.02), 4)
        limit_price = max(0.01, min(0.68, limit_price))
        shares = self._risk.shares_from_usdc(usdc, limit_price)

        if shares < config.MIN_ORDER_SHARES:
            logger.info(
                f"[SIZE] Skipping {_detect_updown_market(sig.question)} {sig.side} — "
                f"kelly_size=${usdc:.2f} → {shares:.2f} shares @ {limit_price:.3f} "
                f"(need {config.MIN_ORDER_SHARES}, edge={sig.edge:.1%}). "
                f"Raise kelly_override or wait for higher-edge signal."
            )
            return False

        kind = "arb" if sig.is_latency_arb else "divergence"
        urgency_tag = ""
        if sig.hours_to_close is not None:
            if sig.hours_to_close <= 24:
                urgency_tag = f" ⚡{sig.hours_to_close:.0f}h"
            elif sig.hours_to_close <= 48:
                urgency_tag = f" {sig.hours_to_close:.0f}h"
        self._dash_state.add_exec_log(kind,
            f"+{sig.edge:.2%} divergence{urgency_tag} — \"{sig.question[:40]}\" "
            f"CLOB @ {sig.market_price:.2f} | fair {sig.fair_value:.2f} via {'ARB' if sig.is_latency_arb else 'MOM+OB'}")

        wallet = self._dash_state.wallet_balance or 0.0
        if wallet > 0 and wallet < usdc * 0.5:
            logger.debug(f"Skipping — wallet ${wallet:.2f} too low for ${usdc:.2f} order")
            return False

        # Order book thinness filter: thick books mean MMs are active and have repriced.
        # Thin books (wide spread) = MMs absent = maximum oracle lag edge.
        # Skip if spread is very tight — means aggressive MMs are dominating the book.
        # For NO-side, use the NO token's book if available; fall back to YES book.
        if sig.side == "NO" and sig.no_best_ask is not None:
            # NO book: estimate spread as no_best_ask - (1 - YES best_ask)
            _no_bid_est = (1.0 - sig.best_ask) if sig.best_ask is not None else 0.0
            # Negative spread = crossed book (exploitable mispricing, not tight MMs).
            # Clamp to 1.0 so the thinness filter allows the trade through.
            _raw = sig.no_best_ask - _no_bid_est if _no_bid_est > 0 else 1.0
            _spread = _raw if _raw > 0 else 1.0
        elif sig.best_ask is not None and sig.best_bid is not None and sig.best_bid > 0:
            _spread = sig.best_ask - sig.best_bid
        else:
            _spread = 1.0  # unknown spread, allow trade
        if _spread < config.OB_MIN_SPREAD:
            logger.debug(
                f"[OB-THINNESS] {sig.question[:40]} — "
                f"spread={_spread:.3f} very tight, MMs repricing fast — skip"
            )
            return False

        # For 5-minute markets, always take liquidity — use ask price + FOK order type
        # so the order fills immediately or cancels. Never post maker orders on short windows:
        # GTC at bid+0.01 will sit on the book and expire when the market resolves (unfilled).
        # The tiny spread saving (~1-2¢) is not worth the risk of zero fill on a 5-min window.
        #
        # ENTRY ASK GUARD: cap the ask we'll pay at ENTRY_PRICE_GUARD (0.54).
        # Without this, a thin-book market with best_ask=0.99 causes the bot to
        # enter at 86-99¢ — instantly a 40%+ loss if price reverts to mid.
        # If the best ask is above the guard, the market has already repriced; skip.
        # For NO-side orders, use the NO token's ask price (not the YES token's ask).
        _entry_ask = sig.no_best_ask if (sig.side == "NO" and sig.no_best_ask) else sig.best_ask
        if _entry_ask is not None and sig.win_mins <= 5:
            _ask_guard = getattr(config, "ENTRY_PRICE_GUARD", 0.54)
            if _entry_ask > _ask_guard:
                logger.info(
                    f"[ASK-GUARD] Skipping — best_ask={_entry_ask:.3f} > {_ask_guard:.2f} "
                    f"(book too expensive to enter safely) {sig.question[:40]}"
                )
                return False
            limit_price = _entry_ask  # cross the spread, fill immediately
            use_fok = True
        elif sig.side == "YES" and sig.best_bid is not None and sig.best_ask is not None:
            # Longer windows: maker order at mid is fine
            _mid = (sig.best_bid + sig.best_ask) / 2
            _maker_price = round(max(sig.best_bid + 0.01, _mid), 2)
            if _maker_price < limit_price:
                logger.debug(
                    f"[LIMIT-OPT] Using maker price {_maker_price:.3f} vs taker "
                    f"{limit_price:.3f} (saves {limit_price - _maker_price:.3f}/share)"
                )
                limit_price = _maker_price
            use_fok = False
        else:
            use_fok = False

        # Polymarket CLOB: maker=USDC (max 2 decimals), taker=shares (max 4 decimals).
        # USDC = shares × price. With fractional shares, 5.10 × 0.51 = 2.601 → rejected.
        # Integer shares × any 2-decimal price always produces a 2-decimal USDC amount.
        import math as _math
        limit_price = round(limit_price, 2)
        shares = float(_math.floor(shares))   # floor to integer — guarantees clean USDC
        if shares < config.MIN_ORDER_SHARES:
            logger.info(f"[SIZE] Skipping after floor — {shares:.0f} shares < {config.MIN_ORDER_SHARES}")
            return False

        _order_start = time.time()
        resp = self._client.place_limit_order(
            token_id=sig.token_id,
            side="BUY",
            price=limit_price,
            size=shares,
            fok=use_fok,
        )
        _order_ms  = int((time.time() - _order_start) * 1000)
        _total_ms  = int((time.time() - _exec_start)  * 1000)
        if _total_ms < 500:
            logger.debug(f"[ADVERSE-SEL] Fast fill ({_total_ms}ms) — watch for adverse selection")
        self._dash_state.add_exec_log("exec",
            f"EXEC ${limit_price:.2f} → \"{sig.question[:38]}\" "
            f"// order={_order_ms}ms total={_total_ms}ms"
            + ("  ⚠ SLOW" if _total_ms > 3000 else ""))
        if _total_ms > 3000:
            logger.warning(
                f"[LATENCY] Signal→order took {_total_ms}ms — oracle lag edge may be gone "
                f"({sig.question[:40]})"
            )

        if resp:
            actual_cost  = usdc
            actual_entry = limit_price
            if isinstance(resp, dict):
                making = resp.get("makingAmount", "")
                taking = resp.get("takingAmount", "")
                try:
                    making_f = float(making) if making else 0.0
                    taking_f = float(taking) if taking else 0.0
                    if making_f > 0:
                        actual_cost = making_f
                    if making_f > 0 and taking_f > 0:
                        actual_entry = making_f / taking_f
                except (ValueError, TypeError):
                    pass
            self._risk.record_open(cost_usdc=actual_cost)
            self._dash_state.orders_placed += 1

            from src.bot import OpenPosition
            self._positions[sig.token_id] = OpenPosition(
                market_id=sig.market_id,
                question=sig.question,
                token_id=sig.token_id,
                side=sig.side,
                shares=shares,
                entry_price=actual_entry,
                cost_usdc=actual_cost,
                momentum_signal=sig.momentum_signal,
                imbalance_signal=sig.imbalance_signal,
                composite_signal=sig.signal,
                confidence=sig.confidence,
                order_id=resp.get("id") if isinstance(resp, dict) else None,
            )
            self._save_positions()

            self._learner.record_open(
                market_id=sig.market_id,
                token_id=sig.token_id,
                side=sig.side,
                question=sig.question,
                entry_price=actual_entry,
                shares=shares,
                cost_usdc=actual_cost,
                momentum_signal=sig.momentum_signal,
                imbalance_signal=sig.imbalance_signal,
                composite_signal=sig.signal,
                confidence=sig.confidence,
                dry_run=config.DRY_RUN,
                rel_strength=getattr(sig, "rel_strength", 0.0),
            )

            if sig.side == "YES":
                sim_entry = sig.best_ask if sig.best_ask and 0.01 < sig.best_ask < 0.99 else limit_price
            else:
                if sig.no_best_ask and 0.01 < sig.no_best_ask < 0.99:
                    sim_entry = sig.no_best_ask
                elif sig.best_bid and 0.05 < sig.best_bid < 0.95:
                    sim_entry = 1.0 - sig.best_bid
                else:
                    sim_entry = limit_price
            sim_entry  = round(max(0.01, min(0.99, sim_entry)), 4)
            sim_shares = round(actual_cost / sim_entry, 4) if sim_entry > 0 else shares
            self._sim.open_position(
                token_id=sig.token_id,
                market_id=sig.market_id,
                question=sig.question,
                side=sig.side,
                entry_price=sim_entry,
                shares=sim_shares,
            )
            self._queue_sim(sig, sim_entry, sim_shares)

            sim_cost_str = f"${actual_cost:.2f}"
            logger.info(
                f"{'[DRY-RUN] Would place' if config.DRY_RUN else 'Placed'} "
                f"BUY {shares:.2f} shares of {sig.token_id[:8]}… @ {limit_price:.4f}"
            )
            logger.info(
                f"{'[SIM] OPEN' if config.DRY_RUN else 'OPEN'}  "
                f"{sig.side} {sim_shares:.2f}@{sim_entry:.4f} = {sim_cost_str}  "
                f"bal=${self._sim._wallet:.2f}  {sig.question[:55]}"
            )
            logger.info(
                f"  Opened {sig.side}: {shares:.2f}@{limit_price:.4f} = {sim_cost_str}  "
                f"[{sig.confidence}]  [t+{sig.secs_into_window:.1f}s into window]"
            )
            return True

        return False

    def _mark_market_closed(self, market_id: str) -> None:
        """Add market to closed set and persist to disk so restarts don't re-process."""
        if market_id:
            self._closed_market_ids.add(market_id)
            _save_closed_market_ids(self._closed_market_ids)

    def _already_positioned(self, market, token_id: str | None = None) -> bool:
        if token_id:
            return token_id in self._positions
        if market.id in self._closed_market_ids:
            return True
        return (
            market.yes_token.token_id in self._positions
            or market.no_token.token_id in self._positions
        )

    # ------------------------------------------------------------------
    # Position persistence
    # ------------------------------------------------------------------

    def _save_positions(self) -> None:
        try:
            _POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_POSITIONS_FILE, "w") as f:
                json.dump({tid: asdict(pos) for tid, pos in self._positions.items()}, f, indent=2)
        except Exception as exc:
            logger.warning(f"_save_positions failed: {exc}")

    def _load_positions(self) -> None:
        if not _POSITIONS_FILE.exists():
            return
        try:
            with open(_POSITIONS_FILE) as f:
                raw = json.load(f)
            from src.bot import OpenPosition
            count = 0
            for token_id, d in raw.items():
                if token_id not in self._positions:
                    fields = {
                        k: v for k, v in d.items()
                        if k in OpenPosition.__dataclass_fields__
                    }
                    if "is_external" not in d:
                        fields["is_external"] = True
                    self._positions[token_id] = OpenPosition(**fields)
                    count += 1
            if count:
                logger.info(f"Restored {count} open position(s) from disk.")
                # Rebuild risk exposure counter from loaded positions so GROUP_CAP
                # is correctly enforced immediately after restart, not just after
                # the first trade cycle.
                for pos in self._positions.values():
                    self._risk.record_open(cost_usdc=getattr(pos, "cost_usdc", 0.0))
        except Exception as exc:
            logger.warning(f"_load_positions failed: {exc}")

    def _reconcile_positions(self) -> None:
        # Lazy-load the persistent redeemed-tokens set
        if self._redeemed_tokens is None:
            self._redeemed_tokens = _load_redeemed()

        try:
            raw_positions = self._client.get_positions()
        except Exception as exc:
            logger.warning(f"_reconcile_positions: could not fetch CLOB positions: {exc}")
            return

        if not raw_positions:
            logger.info("_reconcile_positions: no CLOB positions returned (wallet may be empty)")
            return

        logger.info(f"_reconcile_positions: {len(raw_positions)} position(s) returned — reconciling…")
        from src.bot import OpenPosition
        added = 0
        for raw in raw_positions:
            token_id = (
                raw.get("asset") or
                raw.get("asset_id") or raw.get("assetId") or
                raw.get("token_id") or raw.get("tokenId") or
                raw.get("market") or ""
            )
            try:
                size      = float(raw.get("size", 0) or 0)
                _price_raw = (raw.get("avgPrice") if raw.get("avgPrice") is not None
                              else raw.get("avg_price") if raw.get("avg_price") is not None
                              else raw.get("price"))
                avg_price = float(_price_raw) if _price_raw is not None else 0.5
            except (ValueError, TypeError):
                continue

            if not token_id or size <= 0:
                continue

            # Skip tokens already successfully redeemed (persisted across restarts)
            if token_id in self._redeemed_tokens:
                logger.debug(f"  Skipping already-redeemed token: {token_id[:16]}…")
                continue

            # Skip positions whose market was already claimed/closed (market_id check)
            _early_market_id = str(raw.get("conditionId") or raw.get("market_id") or "")
            if _early_market_id and _early_market_id in self._closed_market_ids:
                logger.debug(f"  Skipping already-closed market: {_early_market_id[:16]}…")
                self._redeemed_tokens.add(token_id)  # cross-populate so token check works next time
                continue

            if token_id in self._positions:
                # Position already tracked — don't touch is_external.
                # Bot-opened positions (is_external=False) must stay that way so
                # early-exit and stop-loss checks apply to them normally.
                logger.debug(f"  Already tracked: {token_id[:16]}…")
                continue

            question  = raw.get("title") or raw.get("question") or ""
            outcome   = raw.get("outcome") or ""
            market_id = str(raw.get("conditionId") or raw.get("market_id") or "")

            if outcome.lower() in ("yes", "up"):
                side = "YES"
            elif outcome.lower() in ("no", "down"):
                side = "NO"
            else:
                side = "YES"

            if not question:
                question = market_id[:20] if market_id else f"[token:{token_id[:16]}]"

            ob = self._client.get_order_book(token_id)
            if ob is not None:
                cur_price = ob.mid
            else:
                cur_price = avg_price

            if cur_price >= 0.97 and not config.DRY_RUN:
                # Winning position — redeem immediately instead of skipping
                logger.info(
                    f"  AUTO-REDEEM: resolved WIN (price={cur_price:.2f}) "
                    f"{size:.2f} shares — {question[:50]}"
                )
                try:
                    mkt = self._client.get_clob_market(market_id) if market_id else None
                    neg_risk = bool(mkt.get("neg_risk", False)) if mkt else False
                except Exception:
                    neg_risk = False
                redeemed_ok = self._client.redeem_position(market_id, neg_risk=neg_risk)
                # Always mark as redeemed regardless of outcome — on-chain it's
                # either paid out or already empty; retrying wastes gas either way.
                self._redeemed_tokens.add(token_id)
                _save_redeemed(self._redeemed_tokens)
                continue

            if cur_price <= 0.03:
                logger.info(f"  Resolved LOSS (price={cur_price:.2f}): {question[:55]}")
                # Record in learner journal so adaptation sees it.
                # Do NOT call risk.record_close here — reconciled losses may have
                # happened in previous sessions and should not inflate today's
                # daily P&L counter (which caused false cap triggers).
                self._learner.record_close(token_id, cur_price)
                self._dash_state.record_closed_trade(0.0, fee_usdc=0.0)
                # Reset the trend tracker so the streak doesn't stay inflated.
                _loss_sym = _detect_updown_market(question)
                if _loss_sym:
                    _loss_dir = "UP" if side == "YES" else "DOWN"
                    self._trend_tracker.record_result(_loss_sym, _loss_dir, won=False)
                # Mark closed so this position never appears in reconcile again.
                if market_id:
                    self._mark_market_closed(market_id)
                self._redeemed_tokens.add(token_id)
                _save_redeemed(self._redeemed_tokens)
                continue


            cost_usdc = avg_price * size
            self._positions[token_id] = OpenPosition(
                market_id=market_id,
                question=question,
                token_id=token_id,
                side=side,
                shares=size,
                entry_price=avg_price,
                cost_usdc=cost_usdc,
                is_external=True,
            )
            added += 1
            logger.info(
                f"  Reconciled {side} {size:.2f}@{avg_price:.4f} "
                f"= ${cost_usdc:.2f}  price={cur_price:.2f} — {question[:55]}"
            )

        if added:
            logger.info(f"_reconcile_positions: added {added} previously-untracked position(s).")
            self._save_positions()
        else:
            logger.info("_reconcile_positions: all CLOB positions are already tracked.")

    # ------------------------------------------------------------------
    # Wallet sync + daily loss guard
    # ------------------------------------------------------------------

    def _sync_wallet_balance(self) -> None:
        balance = self._client.get_usdc_balance()
        if balance is not None and balance > 0:
            self._dash_state.wallet_balance = balance
            if self._dash_state._seed == config.MAX_TOTAL_EXPOSURE_USDC:
                self._dash_state._seed = balance
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
        """Alert when wallet balance drops below $25 — needs USDC top-up."""
        balance = self._dash_state.wallet_balance
        if balance <= 0:
            return
        _REPLENISH_THRESHOLD = 25.0
        if not hasattr(self, "_replenish_alerted"):
            self._replenish_alerted = False
        if balance < _REPLENISH_THRESHOLD and not self._replenish_alerted:
            from src.utils import alerter
            alerter.send(
                f"⚠️ Wallet balance ${balance:.2f} USDC — below $25 threshold. "
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
