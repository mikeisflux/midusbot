"""
Position reconciliation — sync bot state with actual CLOB positions.

ReconcileMixin provides:
  _reconcile_positions — fetch all open positions from Polymarket and add
                         any that the bot doesn't know about yet (e.g. positions
                         opened in a previous session or via the web UI).
"""
from __future__ import annotations

from loguru import logger

from src.strategy import _detect_updown_market
from src.positions.store import save_redeemed
import config


class ReconcileMixin:

    def _reconcile_positions(self) -> None:
        # Lazy-load the persistent redeemed-tokens set
        if self._redeemed_tokens is None:
            from src.positions.store import load_redeemed
            self._redeemed_tokens = load_redeemed()

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

            if token_id in self._redeemed_tokens:
                logger.debug(f"  Skipping already-redeemed token: {token_id[:16]}…")
                continue

            _early_market_id = str(raw.get("conditionId") or raw.get("market_id") or "")
            if _early_market_id and _early_market_id in self._closed_market_ids:
                logger.debug(f"  Skipping already-closed market: {_early_market_id[:16]}…")
                self._redeemed_tokens.add(token_id)
                continue

            if token_id in self._positions:
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
            cur_price = ob.mid if ob is not None else avg_price

            if cur_price >= 0.97 and not config.DRY_RUN:
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
                self._redeemed_tokens.add(token_id)
                save_redeemed(self._redeemed_tokens)
                continue

            if cur_price <= 0.03:
                logger.info(f"  Resolved LOSS (price={cur_price:.2f}): {question[:55]}")
                self._learner.record_close(token_id, cur_price)
                self._dash_state.record_closed_trade(0.0, fee_usdc=0.0)
                _loss_sym = _detect_updown_market(question)
                if _loss_sym:
                    _loss_dir = "UP" if side == "YES" else "DOWN"
                    self._trend_tracker.record_result(_loss_sym, _loss_dir, won=False)
                if market_id:
                    self._mark_market_closed(market_id)
                self._redeemed_tokens.add(token_id)
                save_redeemed(self._redeemed_tokens)
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
