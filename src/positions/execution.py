"""
Trade execution — open and close positions via CLOB or swaps.xyz.

ExecutionMixin provides:
  _execute_signal   — validate signal and place a BUY order
  _close_position   — sell or redeem an open position
"""
from __future__ import annotations

import math as _math
import time
from pathlib import Path

from loguru import logger

from src.strategy import _detect_updown_market, _updown_window_mins
import config


class ExecutionMixin:

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
            _ob_dead = ob is None  # no orderbook = market likely resolved
            if book_best_bid > 0.0:
                sell_price = book_best_bid
            elif current_price is not None and current_price > 0.05:
                sell_price = current_price
            else:
                sell_price = pos.entry_price
            _is_updown = _detect_updown_market(pos.question) is not None
            if not _ob_dead:
                resp = self._client.place_limit_order(
                    token_id=token_id,
                    side="SELL",
                    price=sell_price,
                    size=pos.shares,
                    fok=_is_updown,
                )

        # Redeem fallback: if sell failed/skipped and orderbook is gone, always try redeem.
        # Win positions get paid out; loss positions get $0 but the stuck position is cleared.
        if resp is None and pos.market_id and _ob_dead:
            neg_risk = False
            try:
                mkt = self._client.get_clob_market(pos.market_id)
                if mkt:
                    neg_risk = bool(mkt.get("neg_risk", False))
            except Exception:
                pass
            logger.info(
                f"No orderbook for {pos.question[:40]} — attempting redeem "
                f"(price={current_price:.3f if current_price else 'n/a'})"
            )
            redeemed = self._client.redeem_position(pos.market_id, neg_risk=neg_risk)
            if redeemed:
                resp = {"redeemed": True}
            else:
                logger.warning(
                    f"Redeem failed for {pos.question[:40]} — force-removing stuck position"
                )
                resp = {"force_closed": True}

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

            # Outcome log for offline analysis
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

            # Adverse selection tracking log
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

    def _execute_signal(self, sig) -> bool:
        _exec_start = time.time()
        if config.TRADING_PAUSED:
            return False

        # Cold-start warmup — block trading for 90s after launch while price
        # history accumulates; signals on stale/empty data are unreliable.
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

        # Order book thinness filter
        if sig.side == "NO" and sig.no_best_ask is not None:
            _no_bid_est = (1.0 - sig.best_ask) if sig.best_ask is not None else 0.0
            _raw = sig.no_best_ask - _no_bid_est if _no_bid_est > 0 else 1.0
            _spread = _raw if _raw > 0 else 1.0
        elif sig.best_ask is not None and sig.best_bid is not None and sig.best_bid > 0:
            _spread = sig.best_ask - sig.best_bid
        else:
            _spread = 1.0
        if _spread < config.OB_MIN_SPREAD:
            logger.debug(
                f"[OB-THINNESS] {sig.question[:40]} — "
                f"spread={_spread:.3f} very tight, MMs repricing fast — skip"
            )
            return False

        # Entry ASK guard: cap the ask we'll pay at ENTRY_PRICE_GUARD (0.54).
        # Without this, a thin-book market with best_ask=0.99 causes the bot to
        # enter at 86-99¢ — instantly a 40%+ loss if price reverts to mid.
        _entry_ask = sig.no_best_ask if (sig.side == "NO" and sig.no_best_ask) else sig.best_ask
        if _entry_ask is not None and sig.win_mins <= 5:
            _ask_guard = getattr(config, "ENTRY_PRICE_GUARD", 0.54)
            if _entry_ask > _ask_guard:
                logger.info(
                    f"[ASK-GUARD] Skipping — best_ask={_entry_ask:.3f} > {_ask_guard:.2f} "
                    f"(book too expensive to enter safely) {sig.question[:40]}"
                )
                return False
            limit_price = _entry_ask
            use_fok = True
        elif sig.side == "YES" and sig.best_bid is not None and sig.best_ask is not None:
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

        # Polymarket CLOB: integer shares × 2-decimal price → clean USDC amount
        limit_price = round(limit_price, 2)
        shares = float(_math.floor(shares))
        if shares < config.MIN_ORDER_SHARES:
            logger.info(f"[SIZE] Skipping after floor — {shares:.0f} shares < {config.MIN_ORDER_SHARES}")
            return False

        # ── Final staleness check ─────────────────────────────────────────────
        # Re-fetch live orderbook right before placing the order. Signals are
        # evaluated seconds before execution — market can reprice in that window.
        # We saw entries at 0.31 and 0.40 due to stale signal prices.
        if not config.DRY_RUN:
            _ob_live = self._client.get_order_book(sig.token_id)
            if _ob_live is not None and _ob_live.mid > 0:
                _live_mid = _ob_live.mid
                _guard = config.ENTRY_PRICE_GUARD
                if _live_mid > _guard or _live_mid < (1.0 - _guard):
                    logger.info(
                        f"[STALE-GUARD] Market moved to {_live_mid:.3f} before order "
                        f"— outside [{1-_guard:.2f},{_guard:.2f}] — skip {sig.question[:40]}"
                    )
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
                entry_time=time.time(),
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
