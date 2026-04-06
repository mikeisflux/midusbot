"""
Market Making — 20% portfolio bucket.

Strategy: Post limit BUY orders on BOTH YES and NO sides of UpDown markets
at the bid (below mid), earning the spread vs takers. This is non-directional:
one side resolves to $1.00, the other to $0.00. If both legs fill at 0.47,
total cost = $0.94, guaranteed return = $1.00, profit = 6.4%.

Unlike the dual-arb scanner (which takes from the book at ask), the market
maker POSTS to the book and waits for fills — earning the spread rather than
paying it. Fills are not guaranteed, but the edge is higher when they occur.

Key mechanics:
- Post YES BUY at (best_bid + 0.01) — one tick above current bid
- Post NO BUY at (best_bid + 0.01) on the NO orderbook
- Track pending order IDs in _mm_orders dict
- Cancel unfilled orders before T=210s (90s before window close)
- Positions that fill → tracked with strategy="mm", managed by monitoring.py
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
import config


MM_SPREAD_FROM_MID = 0.03   # post this far below mid (0.50 - 0.03 = 0.47 bid)
MM_MIN_PRICE       = 0.44   # never post below this (too risky, MM spread too wide)
MM_MAX_PRICE       = 0.49   # never post above this (too close to mid = no MM edge)
MM_CANCEL_AT_SECS  = 210    # cancel unfilled MM orders at T=210s (90s before close)
MM_REPOST_INTERVAL = 30     # re-price orders every 30s if mid drifted > 0.02


@dataclass
class MMOrder:
    """Tracks a pending maker order."""
    order_id:  str
    token_id:  str
    market_id: str
    question:  str
    side:      str    # "YES" | "NO"
    price:     float
    shares:    float
    posted_at: float = field(default_factory=time.time)
    secs_into: float = 0.0   # seconds into window when posted


class MarketMakerMixin:
    """
    Mixin for PolymarketBot — adds _run_market_maker() to the main scan loop.
    Manages pending maker orders and promotes fills into tracked positions.
    """

    # Injected by PolymarketBot.__init__ — see bot.py
    # self._client, self._positions, self._risk, self._dash_state, etc.

    def _mm_orders_init(self) -> None:
        """Call from __init__ to initialise MM order tracking."""
        self._mm_orders: dict[str, MMOrder] = {}   # order_id → MMOrder

    def run_market_maker(self, updown_5m: list) -> None:
        """
        Main entry point, called every loop from scanner._loop_once().
        1. Promote any filled MM orders into tracked positions.
        2. Cancel MM orders approaching window close.
        3. Post new MM orders for fresh markets.
        """
        if not hasattr(self, "_mm_orders"):
            self._mm_orders_init()

        _wallet = self._dash_state.wallet_balance or 0.0
        _mm_pct = float(getattr(config, "MM_BUDGET_PCT", 0.20))
        _mm_budget = _wallet * _mm_pct

        _mm_exposure = sum(
            p.cost_usdc for p in self._positions.values()
            if getattr(p, "strategy", "") == "mm"
        ) + sum(o.price * o.shares for o in self._mm_orders.values())

        self._mm_promote_fills()
        self._mm_cancel_stale(updown_5m)
        self._mm_rebalance_inventory()

        if _mm_exposure >= _mm_budget:
            return   # MM bucket full

        remaining = _mm_budget - _mm_exposure
        self._mm_post_orders(updown_5m, remaining)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _mm_promote_fills(self) -> None:
        """
        Check pending MM orders against CLOB open orders.
        Any order no longer in the CLOB has been filled — promote to position.
        """
        if not self._mm_orders:
            return

        import math as _math
        try:
            open_orders = self._client.get_open_orders()
        except Exception as exc:
            logger.debug(f"[MM] get_open_orders failed: {exc}")
            return

        open_ids = {
            o.get("id") or o.get("orderID") or o.get("order_id")
            for o in open_orders
        }

        filled = [o for oid, o in self._mm_orders.items() if oid not in open_ids]
        for order in filled:
            del self._mm_orders[order.order_id]
            # Promote to position
            if order.token_id in self._positions:
                continue   # already tracked (shouldn't happen)

            from src.bot import OpenPosition
            cost = order.price * order.shares
            self._positions[order.token_id] = OpenPosition(
                market_id=order.market_id,
                question=order.question,
                token_id=order.token_id,
                side=order.side,
                shares=order.shares,
                entry_price=order.price,
                cost_usdc=cost,
                entry_time=time.time(),
                strategy="mm",
                confidence="MM",
            )
            self._save_positions()
            self._risk.record_open(cost_usdc=cost)
            logger.info(
                f"[MM] Fill promoted: {order.side} {order.shares:.0f}@{order.price:.3f} "
                f"cost=${cost:.2f}  {order.question[:45]}"
            )

    def _mm_cancel_stale(self, updown_5m: list) -> None:
        """Cancel MM orders that are too close to window close (T > MM_CANCEL_AT_SECS)."""
        from src.strategy import _market_seconds_into_window

        stale = []
        for oid, order in list(self._mm_orders.items()):
            # Find the market to check timing
            market = next(
                (m for m in updown_5m if order.market_id in (
                    getattr(m, "market_id", ""), getattr(m, "id", "")
                )), None
            )
            secs = _market_seconds_into_window(market) if market else None
            if secs is None:
                # Market gone (window closed) — cancel order
                stale.append(oid)
            elif secs > MM_CANCEL_AT_SECS:
                stale.append(oid)

        for oid in stale:
            try:
                ok = self._client.cancel_order(oid)
                if ok:
                    order = self._mm_orders.pop(oid, None)
                    if order:
                        logger.debug(
                            f"[MM] Cancelled stale order {oid[:12]} "
                            f"{order.side}@{order.price:.3f} {order.question[:35]}"
                        )
            except Exception as exc:
                logger.debug(f"[MM] cancel_order {oid[:12]} failed: {exc}")
                self._mm_orders.pop(oid, None)

    def _mm_post_orders(self, updown_5m: list, budget: float) -> None:
        """Post new maker orders on fresh UpDown markets."""
        if config.DRY_RUN:
            self._mm_post_orders_sim(updown_5m, budget)
            return   # Only post real CLOB orders in live mode
        if budget < 2.0:
            return

        from src.strategy import _detect_updown_market, _market_seconds_into_window
        import math as _math

        # Track which markets already have MM orders
        _mm_market_ids = {o.market_id for o in self._mm_orders.values()}

        for market in updown_5m:
            if not self._running:
                break

            mid_id = getattr(market, "market_id", "") or getattr(market, "id", "") or ""
            if mid_id in _mm_market_ids:
                continue   # already posting on this market

            # Only post at the start of a fresh window (T < 60s)
            secs = _market_seconds_into_window(market)
            if secs is None or secs > 60 or secs < 2:
                continue

            # Skip if already have a directional position here
            yes_tid = market.yes_token.token_id
            no_tid  = market.no_token.token_id
            if yes_tid in self._positions or no_tid in self._positions:
                continue

            yes_ob = self._client.get_order_book(yes_tid)
            no_ob  = self._client.get_order_book(no_tid)
            if not yes_ob or not no_ob:
                continue

            # Post just above current bid (maker, not taker)
            yes_bid = yes_ob.best_bid or 0.0
            no_bid  = no_ob.best_bid  or 0.0

            yes_post = round(min(MM_MAX_PRICE, max(MM_MIN_PRICE, yes_bid + 0.01)), 2)
            no_post  = round(min(MM_MAX_PRICE, max(MM_MIN_PRICE, no_bid  + 0.01)), 2)

            # Combined cost guard: must be profitable after 2% Poly fee
            if yes_post + no_post >= 0.96:
                continue   # no spread left after fees

            # Size per leg
            per_leg_usdc = min(budget / 2, config.MAX_POSITION_USDC / 2)
            shares = _math.floor(per_leg_usdc / max(yes_post, no_post))
            if shares < config.MIN_ORDER_SHARES:
                continue

            # Place YES leg (limit, not FOK — maker order)
            resp_yes = self._client.place_limit_order(
                token_id=yes_tid, side="BUY", price=yes_post, size=shares, fok=False
            )
            if resp_yes and isinstance(resp_yes, dict):
                oid_yes = resp_yes.get("id") or resp_yes.get("orderID")
                if oid_yes:
                    self._mm_orders[oid_yes] = MMOrder(
                        order_id=oid_yes, token_id=yes_tid, market_id=mid_id,
                        question=market.question, side="YES",
                        price=yes_post, shares=shares, secs_into=secs,
                    )

            # Place NO leg
            resp_no = self._client.place_limit_order(
                token_id=no_tid, side="BUY", price=no_post, size=shares, fok=False
            )
            if resp_no and isinstance(resp_no, dict):
                oid_no = resp_no.get("id") or resp_no.get("orderID")
                if oid_no:
                    self._mm_orders[oid_no] = MMOrder(
                        order_id=oid_no, token_id=no_tid, market_id=mid_id,
                        question=market.question, side="NO",
                        price=no_post, shares=shares, secs_into=secs,
                    )

            if resp_yes or resp_no:
                logger.info(
                    f"[MM] Posted YES@{yes_post:.3f} + NO@{no_post:.3f} "
                    f"{shares:.0f}shares  {market.question[:45]}"
                )
                _mm_market_ids.add(mid_id)
                budget -= (yes_post + no_post) * shares
                if budget < 2.0:
                    break

    def _mm_post_orders_sim(self, updown_5m: list, budget: float) -> None:
        """
        DRY_RUN simulation of MM order posting.
        Assumes immediate fill at the posted price (optimistic but useful for gauging strategy).
        """
        if budget < 2.0:
            return

        from src.strategy import _detect_updown_market, _market_seconds_into_window
        import math as _math

        _mm_market_ids = {o.market_id for o in self._mm_orders.values()}
        # Also exclude markets where we already have sim positions
        for pos in self._positions.values():
            if getattr(pos, "strategy", "") == "mm":
                _mm_market_ids.add(pos.market_id)

        for market in updown_5m:
            if not self._running:
                break

            mid_id = getattr(market, "market_id", "") or getattr(market, "id", "") or ""
            if mid_id in _mm_market_ids:
                continue

            secs = _market_seconds_into_window(market)
            if secs is None or secs > 60 or secs < 2:
                continue

            yes_tid = market.yes_token.token_id
            no_tid  = market.no_token.token_id
            if yes_tid in self._positions or no_tid in self._positions:
                continue

            yes_ob = self._client.get_order_book(yes_tid)
            no_ob  = self._client.get_order_book(no_tid)
            if not yes_ob or not no_ob:
                continue

            yes_bid = yes_ob.best_bid or 0.0
            no_bid  = no_ob.best_bid  or 0.0
            yes_post = round(min(MM_MAX_PRICE, max(MM_MIN_PRICE, yes_bid + 0.01)), 2)
            no_post  = round(min(MM_MAX_PRICE, max(MM_MIN_PRICE, no_bid  + 0.01)), 2)

            if yes_post + no_post >= 0.96:
                continue

            per_leg_usdc = min(budget / 2, config.MAX_POSITION_USDC / 2)
            shares = _math.floor(per_leg_usdc / max(yes_post, no_post))
            if shares < config.MIN_ORDER_SHARES:
                continue

            hrs = 5 / 60
            self._open_sim_position_raw(
                token_id=yes_tid, market_id=mid_id, question=market.question,
                side="YES", entry=yes_post, shares=shares,
                confidence="MM", hours_to_close=hrs, strategy="mm",
            )
            self._open_sim_position_raw(
                token_id=no_tid, market_id=mid_id, question=market.question,
                side="NO", entry=no_post, shares=shares,
                confidence="MM", hours_to_close=hrs, strategy="mm",
            )
            logger.info(
                f"[MM DRY-RUN] Simulated YES@{yes_post:.3f} + NO@{no_post:.3f} "
                f"{shares:.0f}shares  {market.question[:45]}"
            )
            _mm_market_ids.add(mid_id)
            budget -= (yes_post + no_post) * shares
            if budget < 2.0:
                break

    def _mm_rebalance_inventory(self) -> None:
        """
        Inventory rebalancing: if one leg of a dual MM position fills but the
        other doesn't (or was cancelled), we have unintended directional exposure.
        Detect and hedge by placing an immediate market sell on the filled leg.

        Called from run_market_maker() before posting new orders.
        """
        if config.DRY_RUN:
            return  # no live orders to hedge in sim mode

        # Build a map of filled YES/NO pairs by market_id
        # A leg is "orphaned" if its counterpart order is not in _mm_orders AND
        # it hasn't resolved yet (still in self._positions).
        _pending_market_ids: set[str] = {o.market_id for o in self._mm_orders.values()}

        for token_id, pos in list(self._positions.items()):
            if getattr(pos, "strategy", "") != "mm":
                continue
            if pos.market_id in _pending_market_ids:
                continue  # counterpart order still open — not orphaned yet

            # Check if the counterpart LEG is also a position (both filled = balanced)
            counterpart_side = "NO" if pos.side == "YES" else "YES"
            counterpart_exists = any(
                p.market_id == pos.market_id and p.side == counterpart_side
                for p in self._positions.values()
            )
            if counterpart_exists:
                continue  # both legs filled — balanced, no hedge needed

            # Orphaned single leg — sell it immediately to flatten exposure
            ob = self._client.get_order_book(token_id)
            if not ob or ob.best_bid <= 0:
                continue

            sell_price = round(ob.best_bid - 0.01, 2)
            if sell_price < 0.01:
                continue

            logger.warning(
                f"[MM-REBAL] Orphaned {pos.side} leg detected — hedging "
                f"{pos.shares:.0f} shares @ {sell_price:.3f}  {pos.question[:45]}"
            )
            try:
                resp = self._client.place_limit_order(
                    token_id=token_id, side="SELL",
                    price=sell_price, size=pos.shares, fok=False,
                )
                if resp:
                    logger.info(f"[MM-REBAL] Hedge order placed: {resp}")
            except Exception as exc:
                logger.warning(f"[MM-REBAL] Hedge order failed: {exc}")
