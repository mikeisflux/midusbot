"""
Position management mixin for PolymarketBot.

Contains: OpenPosition dataclass, position lifecycle methods (manage, close,
save, load, reconcile) and the gamma-price fallback helper.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from loguru import logger

from src.client import Market
from src.strategy import _detect_updown_market

import config

if TYPE_CHECKING:
    pass

_POSITIONS_FILE = Path("data/positions.json")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    market_id: str
    question: str
    token_id: str
    side: str
    shares: float
    entry_price: float
    cost_usdc: float
    momentum_signal: float = 0.0
    imbalance_signal: float = 0.0
    composite_signal: float = 0.0
    confidence: str = "LOW"
    order_id: Optional[str] = None
    # True for positions reconciled from external trade history (not opened by
    # this bot session). These are NEVER auto-sold — only manual SELL applies.
    is_external: bool = False


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------

class PositionsMixin:
    """Position tracking, SL/TP management and position lifecycle methods."""

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_positions(self) -> None:
        if not self._positions:
            self._dash_state.positions = []
            return

        to_close: list[tuple[str, float]] = []   # (token_id, current_price)
        position_snapshots: list[tuple[OpenPosition, float]] = []

        for token_id, pos in list(self._positions.items()):
            ob = self._client.get_order_book(token_id)
            book_is_empty = ob is None or (ob.best_bid == 0.0 and ob.best_ask == 1.0)

            # When book is unavailable, fetch the CLOB market for two purposes:
            # 1. Enrich placeholder question if still missing
            # 2. Check if market resolved/closed (prune ghost positions)
            # 3. Get current token price from tokens[] array
            clob_mkt: dict | None = None
            if book_is_empty and pos.market_id:
                try:
                    clob_mkt = self._client.get_clob_market(pos.market_id)
                except Exception:
                    pass

            # Lazily enrich placeholder question
            if clob_mkt:
                q = clob_mkt.get("question", "")
                if q and (pos.question.startswith("[token:") or len(pos.question) <= 20):
                    pos.question = q

            # Prune external (reconciled) positions whose market is confirmed closed.
            # These are settled markets that resolved naturally — no sell trade was
            # ever recorded, so trade-history reconstruction still shows them open.
            if ob is None and pos.is_external and clob_mkt is not None:
                market_active = clob_mkt.get("active", True)
                market_closed = clob_mkt.get("closed", False)
                if not market_active or market_closed:
                    logger.info(
                        f"PRUNED: market resolved/closed — removing ghost position "
                        f"{pos.question[:55]}"
                    )
                    self._risk.record_close()
                    if pos.market_id:
                        self._closed_market_ids.add(pos.market_id)
                    del self._positions[token_id]
                    self._save_positions()
                    continue

            if book_is_empty:
                current_price = self._gamma_position_price(pos)
            else:
                current_price = ob.mid

            # Auto-claim won positions and clear lost positions — runs for ALL
            # positions (including reconciled/external). Only stop-loss and
            # take-profit are skipped for external positions.
            if not config.DRY_RUN:
                # Auto-claim: token resolved in our favour (worth $1.00).
                # Calls on-chain redeemPositions if CLOB orderbook is gone.
                if current_price >= 0.97:
                    logger.info(
                        f"AUTO-CLAIM: resolved YES @ ${current_price:.3f} "
                        f"({pos.shares:.2f} shares)  {pos.question[:50]}"
                    )
                    self._close_position(token_id, current_price=current_price, manual=True)
                    continue

                # Auto-clear: token resolved against us (worth $0.00).
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

            if not pos.is_external:
                if self._risk.should_stop_loss(pnl_pct):
                    logger.warning(f"STOP-LOSS {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))
                elif self._risk.should_take_profit(pnl_pct):
                    logger.info(f"TAKE-PROFIT {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))

        self._dash_state.positions = position_snapshots

        for token_id, cur_price in to_close:
            self._close_position(token_id, current_price=cur_price)

    def _gamma_position_price(self, pos: OpenPosition) -> float:
        """
        When the CLOB order book is empty (market resolved or inactive), fetch
        the current token price from the CLOB markets endpoint (fast, single-shot).
        Falls back to entry_price if all calls fail.
        """
        if pos.market_id:
            try:
                mkt = self._client.get_clob_market(pos.market_id)
                if mkt:
                    tokens = mkt.get("tokens") or []
                    for tok in tokens:
                        if str(tok.get("token_id", "")) == pos.token_id:
                            price = float(tok.get("price") or 0)
                            if price > 0:
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

        # ── Path 1: swaps.xyz (preferred — handles tickSize/negRisk automatically) ──
        if config.SWAPS_API_KEY:
            # Fetch market params for tick_size and neg_risk
            tick_size = "0.01"
            neg_risk  = False
            if pos.market_id:
                try:
                    mkt = self._client.get_clob_market(pos.market_id)
                    if mkt:
                        tick_size = str(mkt.get("minimum_tick_size", "0.01"))
                        neg_risk  = bool(mkt.get("neg_risk", False))
                        # Also update the question if it's still a placeholder
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
                    resp = None  # fall through to CLOB

        # ── Path 2: direct CLOB limit order (fallback) ───────────────────────────
        if resp is None:
            # Single-shot order book — no retries (already fast after recent fix)
            ob = self._client.get_order_book(token_id)
            book_best_bid = ob.best_bid if ob else 0.0
            if book_best_bid > 0.0:
                sell_price = book_best_bid
            elif current_price is not None and current_price > 0.05:
                sell_price = current_price
            else:
                sell_price = pos.entry_price
            resp = self._client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price=sell_price,
                size=pos.shares,
            )

        # ── Path 3: on-chain redemption (resolved market — no orderbook) ─────────
        # When a market resolves the CLOB orderbook disappears. Call redeemPositions
        # on the CTF Exchange directly to convert winning tokens → USDC.
        # pos.market_id IS the condition_id on Polymarket.
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
            # Use current_price for P&L — skip second get_order_book() call
            exit_price = (
                current_price or
                pos.entry_price
            )
            exit_usdc = exit_price * pos.shares
            fee = self._risk.trade_fee(pos.cost_usdc, exit_usdc)
            pnl = self._learner.record_close(token_id, exit_price)
            self._risk.record_close(pnl_usdc=pnl - fee, cost_usdc=pos.cost_usdc)
            self._dash_state.record_closed_trade(pnl, fee_usdc=fee)

            # Update trend tracker for UpDown markets when they fully resolve
            symbol = _detect_updown_market(pos.question)
            if symbol:
                direction_bet = "UP" if pos.side == "YES" else "DOWN"
                if exit_price >= 0.95:
                    self._trend_tracker.record_result(symbol, direction_bet, won=True)
                elif exit_price <= 0.05:
                    self._trend_tracker.record_result(symbol, direction_bet, won=False)
                # intermediate exit (stop-loss/take-profit) — don't update trend

            # Always remove from tracking — in DRY_RUN this is simulated, but
            # we still need to delete so stop-loss/take-profit don't re-fire
            # every loop on the same position.
            if pos.market_id:
                self._closed_market_ids.add(pos.market_id)
            del self._positions[token_id]
            self._save_positions()
            logger.info(f"{'[SIM] ' if config.DRY_RUN else ''}Closed: {pos.side} {pos.question[:40]}  P&L=${pnl:+.2f}  fee=${fee:.4f}  net=${pnl-fee:+.2f}")
            return True

        # All sell paths failed (e.g. closed/resolved market with no order book).
        # For manual clicks, force-remove from tracking — user explicitly wants it
        # gone. Polymarket auto-credits resolved YES winnings to the wallet.
        if manual:
            self._risk.record_close(cost_usdc=pos.cost_usdc)
            del self._positions[token_id]
            self._save_positions()
            logger.info(f"MANUAL-REMOVE: sell order unavailable (market closed?) — removed from tracking: {pos.question[:55]}")
            return True

        return False

    # ------------------------------------------------------------------
    # Position persistence
    # ------------------------------------------------------------------

    def _save_positions(self) -> None:
        """Write open positions to disk so they survive a restart."""
        try:
            _POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_POSITIONS_FILE, "w") as f:
                json.dump({tid: asdict(pos) for tid, pos in self._positions.items()}, f, indent=2)
        except Exception as exc:
            logger.warning(f"_save_positions failed: {exc}")

    def _load_positions(self) -> None:
        """Reload open positions from disk on startup."""
        if not _POSITIONS_FILE.exists():
            return
        try:
            with open(_POSITIONS_FILE) as f:
                raw = json.load(f)
            count = 0
            for token_id, d in raw.items():
                if token_id not in self._positions:
                    fields = {
                        k: v for k, v in d.items()
                        if k in OpenPosition.__dataclass_fields__
                    }
                    # Positions saved before is_external was added default to True
                    # (treat as external/protected) rather than False (bot-managed).
                    # Positions the bot actively opened will have is_external=False
                    # explicitly in the JSON; anything missing the field is safer
                    # to protect from auto-sells until reconcile confirms otherwise.
                    if "is_external" not in d:
                        fields["is_external"] = True
                    self._positions[token_id] = OpenPosition(**fields)
                    count += 1
            if count:
                logger.info(f"Restored {count} open position(s) from disk.")
        except Exception as exc:
            logger.warning(f"_load_positions failed: {exc}")

    def _reconcile_positions(self) -> None:
        """
        Fetch live positions from Polymarket CLOB API and add any not already
        tracked in self._positions. Handles positions opened before persistence
        was added, or in a different session / directly on polymarket.com.
        Always runs regardless of DRY_RUN — existing real positions must be
        visible and manageable even when the bot is in sandbox mode.
        """

        try:
            raw_positions = self._client.get_positions()
        except Exception as exc:
            logger.warning(f"_reconcile_positions: could not fetch CLOB positions: {exc}")
            return

        if not raw_positions:
            logger.info("_reconcile_positions: no CLOB positions returned (wallet may be empty)")
            return

        logger.info(f"_reconcile_positions: {len(raw_positions)} position(s) returned — reconciling…")
        added = 0
        for raw in raw_positions:
            # Data API fields: asset=token_id, title=question, outcome=YES/NO/Up/Down
            # py_clob_client fields: asset_id / assetId / token_id / market
            token_id = (
                raw.get("asset") or
                raw.get("asset_id") or raw.get("assetId") or
                raw.get("token_id") or raw.get("tokenId") or
                raw.get("market") or ""
            )
            try:
                size = float(raw.get("size", 0) or 0)
                avg_price = float(
                    raw.get("avgPrice") or raw.get("avg_price") or
                    raw.get("price") or 0.5
                )
            except (ValueError, TypeError):
                continue

            if not token_id or size <= 0:
                continue

            if token_id in self._positions:
                # Fix any positions loaded from old JSON without is_external=True
                if not self._positions[token_id].is_external:
                    self._positions[token_id].is_external = True
                    logger.info(f"  Fixed is_external=True: {token_id[:16]}…")
                else:
                    logger.debug(f"  Already tracked: {token_id[:16]}…")
                continue

            # Trade data gives us outcome ("Yes"/"No"/"Up"/"Down") and conditionId
            question  = raw.get("title") or raw.get("question") or ""
            outcome   = raw.get("outcome") or ""
            market_id = str(raw.get("conditionId") or raw.get("market_id") or "")

            # Map outcome string to YES/NO side
            if outcome.lower() in ("yes", "up"):
                side = "YES"
            elif outcome.lower() in ("no", "down"):
                side = "NO"
            else:
                side = "YES"  # fallback

            # Use conditionId as placeholder — question gets filled in lazily
            # by _manage_positions() on first loop (via get_clob_market).
            if not question:
                question = market_id[:20] if market_id else f"[token:{token_id[:16]}]"

            # Check current price — skip positions that have already resolved
            # (price at 0.00 = lost, price at 1.00 = won). These show up in
            # trade history but the market is done; adding them inflates exposure.
            ob = self._client.get_order_book(token_id)
            if ob is not None:
                cur_price = ob.mid
            else:
                cur_price = avg_price  # fallback; will be checked next manage loop
            if cur_price >= 0.97 or cur_price <= 0.03:
                logger.info(
                    f"  Skipping resolved position (price={cur_price:.2f}): {question[:55]}"
                )
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
                is_external=True,  # never auto-sold, only manual SELL
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
