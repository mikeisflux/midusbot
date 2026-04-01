"""
Polymarket client — wraps the CLOB API (authenticated trading) and the
Gamma API (market data / price history).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests
from loguru import logger

import config

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Token:
    token_id: str
    outcome: str   # "Yes" | "No"
    price: float


@dataclass
class Market:
    id: str
    question: str
    condition_id: str
    slug: str
    end_date: str
    active: bool
    closed: bool
    volume: float
    liquidity: float
    yes_token: Token
    no_token: Token

    @property
    def yes_price(self) -> float:
        return self.yes_token.price

    @property
    def no_price(self) -> float:
        return self.no_token.price


@dataclass
class OrderBook:
    token_id: str
    bids: list[dict]   # [{"price": "0.64", "size": "100"}, ...]
    asks: list[dict]

    @property
    def best_bid(self) -> float:
        return float(self.bids[0]["price"]) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return float(self.asks[0]["price"]) if self.asks else 1.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid


@dataclass
class PricePoint:
    timestamp: int
    price: float


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    size: float
    avg_price: float
    current_price: float

    @property
    def pnl(self) -> float:
        return self.size * (self.current_price - self.avg_price)

    @property
    def pnl_pct(self) -> float:
        return (self.current_price - self.avg_price) / self.avg_price if self.avg_price else 0.0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PolymarketClient:
    """
    Thin wrapper around the Polymarket CLOB and Gamma APIs.

    • Gamma API  — public, no auth needed — used for market discovery and
                   price history.
    • CLOB API   — requires wallet-derived API credentials — used for order
                   placement, position queries, and order book data.
    """

    _RETRY_DELAYS = (2, 4, 8)  # seconds

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        self._clob_client = self._init_clob_client()

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def _init_clob_client(self):
        if not config.PRIVATE_KEY:
            logger.warning("PRIVATE_KEY not set — running in data-only mode (no trading)")
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            kwargs = dict(
                host=config.CLOB_HOST,
                chain_id=config.CHAIN_ID,
                key=config.PRIVATE_KEY,
                signature_type=config.SIGNATURE_TYPE,
            )
            if config.FUNDER_ADDRESS:
                kwargs["funder"] = config.FUNDER_ADDRESS
            client = ClobClient(**kwargs)

            if config.CLOB_API_KEY and config.CLOB_API_SECRET and config.CLOB_API_PASSPHRASE:
                creds = ApiCreds(
                    api_key=config.CLOB_API_KEY,
                    api_secret=config.CLOB_API_SECRET,
                    api_passphrase=config.CLOB_API_PASSPHRASE,
                )
            else:
                logger.info("Deriving CLOB API credentials from private key (one-time)…")
                creds = client.create_or_derive_api_creds()
                logger.info(
                    f"Credentials derived — add these to .env to skip re-derivation:\n"
                    f"  CLOB_API_KEY={creds.api_key}\n"
                    f"  CLOB_API_SECRET={creds.api_secret}\n"
                    f"  CLOB_API_PASSPHRASE={creds.api_passphrase}"
                )

            client.set_api_creds(creds)
            logger.info("CLOB client initialised.")
            return client

        except Exception as exc:
            logger.error(f"Failed to initialise CLOB client: {exc}")
            return None

    # ------------------------------------------------------------------
    # Generic HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> Any:
        for attempt, delay in enumerate((*self._RETRY_DELAYS, None), 1):
            try:
                resp = self._session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if delay is None:
                    logger.error(f"GET {url} failed after all retries: {exc}")
                    return None
                logger.warning(f"GET {url} attempt {attempt} failed: {exc} — retrying in {delay}s")
                time.sleep(delay)

    # ------------------------------------------------------------------
    # Market data (Gamma API)
    # ------------------------------------------------------------------

    def get_markets(self, limit: int = 200) -> list[Market]:
        """Return active, non-closed markets with sufficient liquidity."""
        import json as _json

        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
        )
        if not data:
            return []

        markets: list[Market] = []
        for raw in data:
            try:
                # Gamma API returns outcomes/prices/tokenIds as JSON strings
                def _parse(field, default="[]"):
                    v = raw.get(field, default)
                    if isinstance(v, str):
                        return _json.loads(v)
                    return v if v else []

                outcomes     = _parse("outcomes")
                prices       = _parse("outcomePrices")
                token_ids    = _parse("clobTokenIds")

                if len(outcomes) < 2 or len(token_ids) < 2:
                    continue

                yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
                no_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() == "no"),  1)

                yes_price = float(prices[yes_idx]) if len(prices) > yes_idx else 0.5
                no_price  = float(prices[no_idx])  if len(prices) > no_idx  else 0.5

                m = Market(
                    id=str(raw.get("id", "")),
                    question=raw.get("question", ""),
                    condition_id=raw.get("conditionId", ""),
                    slug=raw.get("slug", ""),
                    end_date=raw.get("endDate", ""),
                    active=bool(raw.get("active", False)),
                    closed=bool(raw.get("closed", True)),
                    volume=float(raw.get("volumeClob") or raw.get("volume") or 0),
                    liquidity=float(raw.get("liquidityClob") or raw.get("liquidity") or 0),
                    yes_token=Token(
                        token_id=str(token_ids[yes_idx]),
                        outcome="Yes",
                        price=yes_price,
                    ),
                    no_token=Token(
                        token_id=str(token_ids[no_idx]),
                        outcome="No",
                        price=no_price,
                    ),
                )
                markets.append(m)
            except (KeyError, ValueError, TypeError, IndexError) as exc:
                logger.debug(f"Skipping malformed market: {exc}")

        logger.info(f"Fetched {len(markets)} active markets from Gamma API.")
        return markets

    def get_price_history(self, market_id: str, fidelity: int = 60) -> list[PricePoint]:
        """
        Fetch hourly (fidelity=60) price history for the YES token of a market.
        Returns most-recent points last.
        Uses CLOB API with token_id (not Gamma market_id).
        """
        data = self._get(
            f"{config.CLOB_HOST}/prices-history",
            params={"token_id": market_id, "interval": "1d", "fidelity": fidelity},
        )
        if not data or "history" not in data:
            return []

        return [
            PricePoint(timestamp=int(p.get("t", 0)), price=float(p.get("p", 0.5)))
            for p in data["history"]
        ]

    # ------------------------------------------------------------------
    # Order book (CLOB API — public endpoint)
    # ------------------------------------------------------------------

    def get_order_book(self, token_id: str) -> OrderBook | None:
        data = self._get(
            f"{config.CLOB_HOST}/book",
            params={"token_id": token_id},
        )
        if not data:
            return None

        return OrderBook(
            token_id=token_id,
            bids=data.get("bids", []),
            asks=data.get("asks", []),
        )

    # ------------------------------------------------------------------
    # Authenticated operations (require CLOB client + private key)
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict]:
        """Return open CLOB positions for the connected wallet."""
        if not self._clob_client:
            return []
        try:
            return self._clob_client.get_positions() or []
        except Exception as exc:
            logger.error(f"get_positions failed: {exc}")
            return []

    def get_open_orders(self) -> list[dict]:
        if not self._clob_client:
            return []
        try:
            return self._clob_client.get_orders() or []
        except Exception as exc:
            logger.error(f"get_open_orders failed: {exc}")
            return []

    def place_limit_order(
        self,
        token_id: str,
        side: str,   # "BUY" | "SELL"
        price: float,
        size: float,
    ) -> dict | None:
        """
        Place a GTC limit order.
        Returns the order dict on success, None on failure.
        price — probability (0–1), e.g. 0.65 means 65 ¢ per share
        size  — number of shares (= USDC spent when buying at `price`)
        """
        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Would place {side} {size:.2f} shares of {token_id[:8]}… @ {price:.4f}")
            return {"dry_run": True, "side": side, "price": price, "size": size}

        if not self._clob_client:
            logger.error("CLOB client not available — cannot place order.")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, Side

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=Side.BUY if side == "BUY" else Side.SELL,
            )
            signed_order = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed_order, OrderType.GTC)
            logger.info(f"Order placed: {resp}")
            return resp
        except Exception as exc:
            logger.error(f"place_limit_order failed: {exc}")
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

    def cancel_all_orders(self) -> None:
        for order in self.get_open_orders():
            oid = order.get("id") or order.get("order_id")
            if oid:
                self.cancel_order(oid)
