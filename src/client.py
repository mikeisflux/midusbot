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
from src.utils import CircuitBreaker

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

    # Circuit breakers — open after 5 consecutive failures, reset after 60s
    _cb_gamma = CircuitBreaker("Gamma API", failure_threshold=5, reset_secs=60)
    _cb_clob  = CircuitBreaker("CLOB API",  failure_threshold=5, reset_secs=60)

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
        cb = self._cb_clob if "clob.polymarket" in url else self._cb_gamma
        if not cb.allow():
            return None
        for attempt, delay in enumerate((*self._RETRY_DELAYS, None), 1):
            try:
                resp = self._session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                cb.success()
                return resp.json()
            except Exception as exc:
                if delay is None:
                    cb.failure()
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

        # Keywords that identify sports/team betting markets — never trade these
        _SPORTS_KEYWORDS = (
            " nhl ", " nba ", " nfl ", " mlb ", " nhl\n", " nba\n",
            "stanley cup", "nba finals", "nfl season", "super bowl",
            "world series", "march madness", "champions league",
            "premier league", "la liga", "serie a", "bundesliga",
            "win the 2025 nhl", "win the 2026 nhl",
            "win the 2025 nba", "win the 2026 nba",
            "win the 2025 nfl", "win the 2026 nfl",
            "win the super bowl", "win the world series",
        )

        # Sort by volume descending so high-activity markets (e.g. "BTC 5 Minute
        # Up or Down" with $35M vol) always appear regardless of how many total
        # markets exist. Limit raised to 500 to cover more ground.
        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={
                "active":    "true",
                "closed":    "false",
                "limit":     max(limit, 500),
                "order":     "volumeClob",
                "ascending": "false",
            },
        )
        if not data:
            return []

        markets: list[Market] = []
        sports_skipped = 0
        for raw in data:
            try:
                question_lower = (raw.get("question") or "").lower()
                if any(kw in question_lower for kw in _SPORTS_KEYWORDS):
                    sports_skipped += 1
                    continue
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

                yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("yes", "up")),   0)
                no_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("no", "down")), 1)

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

    def get_market_by_id(self, market_id: str) -> Market | None:
        """
        Fetch a single market from the Gamma API by its numeric market ID.
        Used to get the resolved outcome price when the CLOB order book is empty.
        """
        import json as _json
        data = self._get(f"{config.GAMMA_HOST}/markets/{market_id}")
        if not data:
            return None
        # Gamma returns the market object directly (not a list) for /markets/{id}
        raw = data if isinstance(data, dict) else (data[0] if isinstance(data, list) and data else None)
        if not raw:
            return None
        try:
            def _parse(field, default="[]"):
                v = raw.get(field, default)
                if isinstance(v, str):
                    return _json.loads(v)
                return v if v else []

            outcomes  = _parse("outcomes")
            prices    = _parse("outcomePrices")
            token_ids = _parse("clobTokenIds")

            if len(outcomes) < 2 or len(token_ids) < 2:
                return None

            yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("yes", "up")), 0)
            no_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("no", "down")), 1)

            yes_price = float(prices[yes_idx]) if len(prices) > yes_idx else 0.5
            no_price  = float(prices[no_idx])  if len(prices) > no_idx  else 0.5

            return Market(
                id=str(raw.get("id", market_id)),
                question=raw.get("question", ""),
                condition_id=raw.get("conditionId", ""),
                slug=raw.get("slug", ""),
                end_date=raw.get("endDate", ""),
                active=bool(raw.get("active", False)),
                closed=bool(raw.get("closed", False)),
                volume=float(raw.get("volumeClob") or raw.get("volume") or 0),
                liquidity=float(raw.get("liquidityClob") or raw.get("liquidity") or 0),
                yes_token=Token(token_id=str(token_ids[yes_idx]), outcome="Yes", price=yes_price),
                no_token=Token(token_id=str(token_ids[no_idx]),  outcome="No",  price=no_price),
            )
        except Exception as exc:
            logger.warning(f"get_market_by_id({market_id}) parse error: {exc}")
            return None

    def get_market_by_clob_token_id(self, token_id: str) -> Market | None:
        """
        Find a market by its CLOB token ID via the Gamma API.
        Used during position reconciliation to look up market info for positions
        opened before persistence was added.
        """
        import json as _json
        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={"clob_token_ids": _json.dumps([token_id])},
        )
        if not data or not isinstance(data, list):
            return None
        for raw in data:
            try:
                def _parse(field, default="[]"):
                    v = raw.get(field, default)
                    if isinstance(v, str):
                        return _json.loads(v)
                    return v if v else []

                outcomes  = _parse("outcomes")
                prices    = _parse("outcomePrices")
                token_ids = _parse("clobTokenIds")

                if token_id not in token_ids:
                    continue
                if len(outcomes) < 2 or len(token_ids) < 2:
                    continue

                yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("yes", "up")), 0)
                no_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("no", "down")), 1)

                yes_price = float(prices[yes_idx]) if len(prices) > yes_idx else 0.5
                no_price  = float(prices[no_idx])  if len(prices) > no_idx  else 0.5

                return Market(
                    id=str(raw.get("id", "")),
                    question=raw.get("question", ""),
                    condition_id=raw.get("conditionId", ""),
                    slug=raw.get("slug", ""),
                    end_date=raw.get("endDate", ""),
                    active=bool(raw.get("active", False)),
                    closed=bool(raw.get("closed", False)),
                    volume=float(raw.get("volumeClob") or raw.get("volume") or 0),
                    liquidity=float(raw.get("liquidityClob") or raw.get("liquidity") or 0),
                    yes_token=Token(token_id=str(token_ids[yes_idx]), outcome="Yes", price=yes_price),
                    no_token=Token(token_id=str(token_ids[no_idx]),  outcome="No",  price=no_price),
                )
            except Exception as exc:
                logger.debug(f"get_market_by_clob_token_id({token_id[:12]}) parse error: {exc}")
        return None

    def get_updown_markets(self) -> list[Market]:
        """
        Fetch short-interval Up/Down crypto markets using multiple strategies:
        1. Date-range query: markets closing in the next 24 hours
        2. Text search fallback: search for "up or down" if date-range yields nothing
        All results are filtered locally via _detect_updown_market.
        """
        import json as _json
        from datetime import datetime, timezone, timedelta
        from src.strategy import _detect_updown_market

        def _parse_market(raw: dict) -> "Market | None":
            question = raw.get("question", "")
            if not _detect_updown_market(question):
                return None
            try:
                def _parse(field, default="[]"):
                    v = raw.get(field, default)
                    if isinstance(v, str):
                        return _json.loads(v)
                    return v if v else []

                outcomes  = _parse("outcomes")
                prices    = _parse("outcomePrices")
                token_ids = _parse("clobTokenIds")

                if len(outcomes) < 2 or len(token_ids) < 2:
                    return None

                yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("yes", "up")), 0)
                no_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("no", "down")), 1)

                yes_price = float(prices[yes_idx]) if len(prices) > yes_idx else 0.5
                no_price  = float(prices[no_idx])  if len(prices) > no_idx  else 0.5

                return Market(
                    id=str(raw.get("id", "")),
                    question=question,
                    condition_id=raw.get("conditionId", ""),
                    slug=raw.get("slug", ""),
                    end_date=raw.get("endDate", ""),
                    active=bool(raw.get("active", False)),
                    closed=bool(raw.get("closed", False)),
                    volume=float(raw.get("volumeClob") or raw.get("volume") or 0),
                    liquidity=float(raw.get("liquidityClob") or raw.get("liquidity") or 0),
                    yes_token=Token(token_id=str(token_ids[yes_idx]), outcome="Up",   price=yes_price),
                    no_token=Token(token_id=str(token_ids[no_idx]),  outcome="Down", price=no_price),
                )
            except Exception as exc:
                logger.debug(f"Skipping malformed up/down market: {exc}")
                return None

        def _collect(data: list | None) -> list:
            if not data:
                return []
            seen: set[str] = set()
            results = []
            for raw in data:
                m = _parse_market(raw)
                if m and m.id and m.id not in seen:
                    seen.add(m.id)
                    results.append(m)
            return results

        now     = datetime.now(timezone.utc)
        end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Strategy 1: date-range query — markets closing in the next 24 hours.
        # A 24h window catches both imminent slots AND upcoming batches Polymarket
        # pre-creates (slots are typically created a few hours in advance).
        end_max_24h = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={
                "active":       "true",
                "closed":       "false",
                "limit":        500,
                "end_date_min": end_min,
                "end_date_max": end_max_24h,
            },
        )
        markets = _collect(data)
        if markets:
            logger.info(f"Fetched {len(markets)} Up/Down crypto markets (date-range).")
            return markets

        # Strategy 2: text search — covers multiple naming conventions:
        #   Old per-slot: "XRP Up or Down - April 2, 2:55AM-3:00AM ET"
        #   New perpetual: "BTC 5 Minute Up or Down", "Bitcoin Up or Down on April 2?"
        #   Hourly:        "BTC 1 Hour Up or Down"
        all_found: list = []
        seen_ids: set[str] = set()
        for search_term in (
            "up or down",       # catches both old and new formats
            "5 minute",         # "BTC 5 Minute Up or Down"
            "15 minute",        # "BTC 15 Minute Up or Down"
            "1 hour",           # "BTC 1 Hour Up or Down"
        ):
            data = self._get(
                f"{config.GAMMA_HOST}/markets",
                params={"active": "true", "closed": "false", "limit": 500, "search": search_term},
            )
            for m in _collect(data):
                if m.id not in seen_ids:
                    seen_ids.add(m.id)
                    all_found.append(m)
        if all_found:
            logger.info(f"Fetched {len(all_found)} Up/Down crypto markets (text search).")
            return all_found

        # Strategy 3: sort active markets by soonest end date (future only).
        # end_date_min=now ensures we only see markets that haven't resolved yet.
        # UpDown 5-min markets expire first so they surface at the top.
        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={
                "active":       "true",
                "closed":       "false",
                "limit":        500,
                "order":        "endDate",
                "ascending":    "true",
                "end_date_min": end_min,   # future markets only — skip resolved stale entries
            },
        )
        markets = _collect(data)
        if markets:
            logger.info(f"Fetched {len(markets)} Up/Down crypto markets (soonest-first scan).")
            return markets

        logger.debug("No Up/Down crypto markets found — no active slots right now.")
        return []

    def get_price_history(self, market_id: str, fidelity: int = 60) -> list[PricePoint]:
        """
        Fetch hourly (fidelity=60) price history for the YES token of a market.
        Returns most-recent points last.
        Uses CLOB API with token_id (not Gamma market_id).
        """
        data = self._get(
            f"{config.CLOB_HOST}/prices-history",
            params={"market": market_id, "interval": "1d", "fidelity": fidelity},
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
        # Single-shot, no retries — called every loop for every position.
        # 404 = market resolved (expected); no point retrying.
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

    # ------------------------------------------------------------------
    # Authenticated operations (require CLOB client + private key)
    # ------------------------------------------------------------------

    def get_usdc_balance(self) -> float | None:
        """
        Fetch the live USDC balance from the Polymarket CLOB API.
        USDC on Polygon has 6 decimals, so raw value 1000000 = $1.00.
        Returns the balance in human-readable USDC (e.g. 250.00).
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
            # Some versions return an already-scaled float; others return raw integer string
            balance = float(raw)
            # Heuristic: if it looks like a raw 6-decimal value (> 1,000,000) scale down
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
          1. Fetch all CONFIRMED trades via the authenticated CLOB API.
          2. Group by token (asset_id): net_shares = sum(BUY) - sum(SELL).
          3. For tokens with net_shares > 0.01 return synthetic position dicts
             with the same field names that _reconcile_positions() expects.

        Falls back to data-api.polymarket.com/positions if the trade history
        call fails (e.g. CLOB client not initialised).
        """
        address = config.FUNDER_ADDRESS.lower() if config.FUNDER_ADDRESS else ""

        # ── 1. Try authenticated trade history ──────────────────────────────
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

        # If py_clob_client gave nothing, try the REST trade history endpoint
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

        # ── 2. Aggregate into net positions ─────────────────────────────────
        if trades:
            from collections import defaultdict
            buys: dict[str, list[tuple[float, float]]] = defaultdict(list)
            sells: dict[str, float] = defaultdict(float)
            meta: dict[str, dict] = {}  # token_id -> {outcome, conditionId}

            for t in trades:
                if t.get("status") not in ("CONFIRMED", "MINED", "MATCHED", None, ""):
                    continue
                tid = t.get("asset_id") or t.get("assetId") or ""
                if not tid:
                    continue
                try:
                    sz = float(t.get("size", 0) or 0)
                    pr = float(t.get("price", 0) or 0)
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
            # Only return here if we actually found positions. If all trades netted
            # to 0 (e.g. all previously sold) or UI-placed positions aren't in the
            # CLOB trade history, fall through to the data-api below.
            if positions:
                return positions
            logger.info("get_positions: 0 net open from trade history — trying data-api")

        # ── 3. data-api.polymarket.com — catches UI-placed positions and any
        #        positions not in the CLOB trade history ─────────────────────
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

    def get_clob_market(self, condition_id: str) -> dict | None:
        """
        Fetch a single market from the CLOB public API by condition ID.
        Returns the raw dict including: question, neg_risk, minimum_tick_size, tokens[].
        Uses a single attempt with a short timeout — never retries, never blocks.
        """
        try:
            resp = self._session.get(
                f"{config.CLOB_HOST}/markets/{condition_id}",
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("condition_id"):
                return data
        except Exception as exc:
            if "404" not in str(exc):  # 404 = resolved market, expected
                logger.debug(f"get_clob_market({condition_id[:16]}) failed: {exc}")
        return None

    def redeem_position(self, condition_id: str, neg_risk: bool = False) -> bool:
        """
        Redeem winning tokens for USDC after a market resolves.

        Calls the CTF Exchange's redeemPositions function via py_clob_client.
        Pass index_sets=[1,2] — the contract only pays out for whichever
        outcome actually won; the other contributes $0.

        For negRisk markets the contract is different but the py_clob_client
        handles routing internally.
        """
        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Would redeem condition {condition_id[:16]}…")
            return True
        if not self._clob_client:
            logger.warning("redeem_position: no CLOB client — cannot redeem")
            return False
        try:
            # Polymarket uses bridged USDC on Polygon as collateral
            USDC    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            ZERO32  = "0x" + "0" * 64
            result  = self._clob_client.redeem_positions(
                USDC, ZERO32, condition_id, [1, 2]
            )
            logger.info(f"Redeemed position — condition {condition_id[:16]}…  result={result}")
            return True
        except Exception as exc:
            logger.warning(f"redeem_position failed: {exc}")
            return False

    def sell_via_swaps(
        self,
        token_id: str,
        amount: float,
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> dict | None:
        """
        Sell a position using the swaps.xyz Workflows API.
        This handles order signing, negRisk routing, and tick-size compliance
        automatically — much simpler than direct CLOB order management.
        Returns the response dict on success, None on failure.
        """
        if not config.SWAPS_API_KEY:
            return None
        eoa = config.EVM_EOA
        if not eoa:
            logger.warning("sell_via_swaps: EVM_EOA not configured")
            return None
        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Would sell {amount:.4f} shares of {token_id[:12]}… via swaps.xyz")
            return {"dry_run": True, "orderResponse": {"success": True}}
        try:
            resp = self._session.post(
                "https://api-v2.swaps.xyz/api/workflows/polymarket/sellPosition",
                headers={
                    "x-api-key": config.SWAPS_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "evmEoa":     eoa,
                    "side":       "SELL",
                    "tokenID":    token_id,
                    "orderType":  "FAK",
                    "tickSize":   tick_size,
                    "negRisk":    neg_risk,
                    "amount":     amount,
                    "feeRateBps": 0,
                    "slippage":   100,
                },
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"swaps.xyz sell: {result}")
            return result
        except Exception as exc:
            logger.error(f"sell_via_swaps failed: {exc}")
            return None

    def get_open_orders(self) -> list[dict]:
        if not self._clob_client:
            return []
        try:
            result = self._clob_client.get_orders() or []
            # py_clob_client may return a dict with 'data' key
            if isinstance(result, dict):
                result = result.get("data", []) or []
            return result
        except Exception as exc:
            logger.error(f"get_open_orders failed: {exc}")
            return []

    def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        cancelled = 0
        try:
            # Try the bulk cancel endpoint first
            if self._clob_client:
                try:
                    self._clob_client.cancel_all()
                    logger.info("cancel_all() called on CLOB client.")
                    return -1  # unknown count but done
                except Exception:
                    pass
            # Fall back to individual cancels
            for order in self.get_open_orders():
                oid = order.get("id") or order.get("orderID") or order.get("order_id")
                if oid:
                    try:
                        self._clob_client.cancel_order(oid)
                        cancelled += 1
                    except Exception as exc:
                        logger.warning(f"cancel_order {oid} failed: {exc}")
        except Exception as exc:
            logger.error(f"cancel_all_orders failed: {exc}")
        return cancelled

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

        # Polygon gas spike handling: check if recent orders failed and retry
        # with small backoff. Polygon congestion causes silent order failures.
        _retries = 2
        _delay   = 1.5
        for _attempt in range(_retries + 1):
            try:
                from py_clob_client.clob_types import OrderArgs, OrderType

                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=side,   # "BUY" or "SELL" string — py_clob_client accepts both
                )
                signed_order = self._clob_client.create_order(order_args)
                resp = self._clob_client.post_order(signed_order, OrderType.GTC)
                logger.info(f"Order placed: {resp}")
                return resp
            except Exception as exc:
                err_str = str(exc).lower()
                # Detect gas / nonce / network errors specifically
                is_gas_err = any(k in err_str for k in (
                    "gas", "nonce", "transaction", "network", "timeout",
                    "connection", "503", "502", "504", "too many"
                ))
                if _attempt < _retries and is_gas_err:
                    logger.warning(
                        f"place_limit_order attempt {_attempt + 1} failed (gas/network): {exc} — "
                        f"retrying in {_delay}s"
                    )
                    import time as _t; _t.sleep(_delay)
                    _delay *= 2   # exponential backoff
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

