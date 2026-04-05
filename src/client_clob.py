"""
CLOB API mixin — authenticated trading operations.
Mixed into PolymarketClient.
"""
from __future__ import annotations

import time
from collections import defaultdict

from loguru import logger

import config
from src.client_types import OrderBook


class ClobMixin:
    """Mixin providing authenticated CLOB API methods."""

    # _get(), _session, and _clob_client must be provided by the host class.

    # ------------------------------------------------------------------
    # Order book (public endpoint)
    # ------------------------------------------------------------------

    def get_order_book(self, token_id: str) -> OrderBook | None:
        """Single-shot order book fetch — no retries."""
        try:
            resp = self._session.get(
                f"{config.CLOB_HOST}/book",
                params={"token_id": token_id},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            return OrderBook(
                token_id=token_id,
                bids=data.get("bids", []),
                asks=data.get("asks", []),
            )
        except Exception:
            return None

    def get_clob_market(self, condition_id: str) -> dict | None:
        """
        Fetch a single market from the CLOB public API by condition ID.
        Returns the raw dict including: question, neg_risk, minimum_tick_size, tokens[].
        Single attempt, short timeout — never blocks.
        """
        try:
            resp = self._session.get(
                f"{config.CLOB_HOST}/markets/{condition_id}",
                timeout=5,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if "404" not in str(exc):
                logger.debug(f"get_clob_market({condition_id[:16]}) failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Authenticated CLOB methods
    # ------------------------------------------------------------------

    def get_usdc_balance(self) -> float | None:
        """
        Fetch the live USDC balance from the Polymarket CLOB API.
        USDC on Polygon has 6 decimals; raw value 1000000 = $1.00.
        Returns None if unauthenticated or the call fails.
        """
        if not self._clob_client:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            resp = self._clob_client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw = resp.get("balance", "0")
            balance = float(raw)
            if balance > 100_000:
                balance = balance / 1_000_000
            return round(balance, 2)
        except Exception as exc:
            logger.error(f"get_usdc_balance failed: {exc}")
            return None

    def get_positions(self) -> list[dict]:
        """
        Reconstruct open positions from confirmed trade history.

        Algorithm:
          1. Fetch confirmed trades via authenticated CLOB API.
          2. Group by token: net_shares = sum(BUY) - sum(SELL).
          3. Tokens with net_shares > 0.01 → synthetic position dicts.

        Falls back to data-api.polymarket.com/positions if trade history fails.
        """
        address = config.FUNDER_ADDRESS.lower() if config.FUNDER_ADDRESS else ""

        # ── 1. Authenticated trade history ────────────────────────────────────
        trades: list[dict] = []
        if self._clob_client:
            try:
                raw = self._clob_client.get_trades() or {}
                if isinstance(raw, list):
                    trades = raw
                elif isinstance(raw, dict):
                    for key in ("data", "trades", "results"):
                        if key in raw and isinstance(raw[key], list):
                            trades = raw[key]
                            break
                logger.info(f"get_positions: {len(trades)} trade(s) from py_clob_client")
            except Exception as exc:
                logger.warning(f"get_positions: py_clob_client.get_trades() failed: {exc}")

        if not trades and address:
            try:
                raw = self._get(
                    f"{config.CLOB_HOST}/data/tradeHistory",
                    params={"maker_address": address, "limit": "500"},
                )
                if isinstance(raw, list):
                    trades = raw
                elif isinstance(raw, dict):
                    for key in ("data", "trades", "results"):
                        if key in raw and isinstance(raw[key], list):
                            trades = raw[key]
                            break
                logger.info(f"get_positions: {len(trades)} trade(s) from REST tradeHistory")
            except Exception as exc:
                logger.warning(f"get_positions: REST tradeHistory failed: {exc}")

        # ── 2. Aggregate into net positions ───────────────────────────────────
        if trades:
            buys: dict[str, list[tuple[float, float]]] = defaultdict(list)
            sells: dict[str, float] = defaultdict(float)
            meta: dict[str, dict] = {}

            for t in trades:
                if t.get("status") not in ("CONFIRMED", "MINED", "MATCHED", None, ""):
                    continue
                tid = t.get("asset_id") or t.get("assetId") or ""
                if not tid:
                    continue
                try:
                    sz = float(t.get("size") or 0)
                    _pr = t.get("price")
                    pr = float(_pr) if _pr is not None else 0.0
                except (ValueError, TypeError):
                    continue
                side = (t.get("side") or "").upper()
                if side == "BUY":
                    buys[tid].append((sz, pr))
                elif side == "SELL":
                    sells[tid] += sz
                meta[tid] = {
                    "outcome":     t.get("outcome") or "",
                    "conditionId": t.get("market") or "",
                }

            positions = []
            for token_id, buy_list in buys.items():
                total_buy  = sum(s for s, _ in buy_list)
                total_sell = sells.get(token_id, 0.0)
                net        = round(total_buy - total_sell, 6)
                if net < 0.01:
                    continue
                avg_price = (
                    sum(s * p for s, p in buy_list) / total_buy
                    if total_buy > 0 else 0.5
                )
                m = meta.get(token_id, {})
                positions.append({
                    "asset":       token_id,
                    "size":        net,
                    "avgPrice":    avg_price,
                    "outcome":     m.get("outcome", ""),
                    "conditionId": m.get("conditionId", ""),
                })
            logger.info(f"get_positions: {len(positions)} net open position(s) from trade history")
            if positions:
                return positions
            logger.info("get_positions: 0 net open from trade history — trying data-api")

        # ── 3. data-api.polymarket.com fallback ───────────────────────────────
        if address:
            for addr_fmt in (address, address.lower(), address.upper()):
                try:
                    data = self._get(
                        "https://data-api.polymarket.com/positions",
                        params={"user": addr_fmt, "limit": "500"},
                    )
                    logger.info(
                        f"get_positions data-api ({addr_fmt[:10]}…): "
                        f"type={type(data).__name__} preview={str(data)[:300]}"
                    )
                    if isinstance(data, list) and data:
                        return data
                    if isinstance(data, dict):
                        for key in ("data", "results", "positions"):
                            if key in data and isinstance(data[key], list) and data[key]:
                                return data[key]
                except Exception as exc:
                    logger.warning(f"get_positions data-api ({addr_fmt[:10]}…) failed: {exc}")

        logger.warning("get_positions: all methods exhausted — returning empty list")
        return []

    def get_open_orders(self) -> list[dict]:
        if not self._clob_client:
            return []
        try:
            result = self._clob_client.get_orders() or []
            if isinstance(result, dict):
                result = result.get("data", []) or []
            return result
        except Exception as exc:
            logger.error(f"get_open_orders failed: {exc}")
            return []

    def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count cancelled (-1 = bulk cancel done)."""
        try:
            if self._clob_client:
                try:
                    self._clob_client.cancel_all()
                    logger.info("cancel_all() called on CLOB client.")
                    return -1
                except Exception:
                    pass
            cancelled = 0
            for order in self.get_open_orders():
                oid = order.get("id") or order.get("orderID") or order.get("order_id")
                if oid:
                    try:
                        self._clob_client.cancel_order(oid)
                        cancelled += 1
                    except Exception as exc:
                        logger.warning(f"cancel_order {oid} failed: {exc}")
            return cancelled
        except Exception as exc:
            logger.error(f"cancel_all_orders failed: {exc}")
            return 0

    def place_limit_order(
        self,
        token_id: str,
        side: str,        # "BUY" | "SELL"
        price: float,
        size: float,
        fok: bool = False,
    ) -> dict | None:
        """
        Place a limit order. Default GTC; pass fok=True for Fill-or-Kill.
        FOK fills immediately at price or cancels — required for 5-min markets
        where a resting GTC order will expire unfilled when the market resolves.
        price — probability (0–1), e.g. 0.65 means 65 ¢ per share
        size  — number of shares (= USDC spent when buying at `price`)
        """
        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Would place {side} {size:.2f} shares of {token_id[:8]}… @ {price:.4f}")
            return {"dry_run": True, "side": side, "price": price, "size": size}

        if not (0.0 < price < 1.0):
            logger.error(f"place_limit_order: price {price} out of range (0,1) — dropping order")
            return None
        if size <= 0:
            logger.error(f"place_limit_order: size {size} must be > 0 — dropping order")
            return None
        if side not in ("BUY", "SELL"):
            logger.error(f"place_limit_order: invalid side '{side}' — dropping order")
            return None

        if not self._clob_client:
            logger.error("CLOB client not available — cannot place order.")
            return None

        _retries = 2
        _delay   = 1.5
        for _attempt in range(_retries + 1):
            try:
                from py_clob_client.clob_types import OrderArgs, OrderType
                order_args   = OrderArgs(token_id=token_id, price=price, size=size, side=side)
                signed_order = self._clob_client.create_order(order_args)
                order_type   = OrderType.FOK if fok else OrderType.GTC
                resp         = self._clob_client.post_order(signed_order, order_type)
                logger.info(f"Order placed: {resp}")
                return resp
            except Exception as exc:
                err_str = str(exc).lower()
                is_gas_err = any(k in err_str for k in (
                    "gas", "nonce", "transaction", "network", "timeout",
                    "connection", "503", "502", "504", "too many",
                ))
                if _attempt < _retries and is_gas_err:
                    logger.warning(
                        f"place_limit_order attempt {_attempt + 1} failed (gas/network): {exc} — "
                        f"retrying in {_delay}s"
                    )
                    time.sleep(_delay)
                    _delay *= 2
                else:
                    logger.error(f"place_limit_order failed after {_attempt + 1} attempt(s): {exc}")
                    return None
        return None

    def cancel_order(self, order_id: str) -> bool:
        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Would cancel order {order_id}")
            return True
        if not self._clob_client:
            return False
        try:
            self._clob_client.cancel_order(order_id)
            return True
        except Exception as exc:
            logger.error(f"cancel_order {order_id} failed: {exc}")
            return False
