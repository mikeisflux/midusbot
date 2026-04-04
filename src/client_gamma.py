"""
Gamma API mixin — public market data (no auth required).
Mixed into PolymarketClient.
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from loguru import logger

import config
from src.client_types import Market, Token, PricePoint

if TYPE_CHECKING:
    pass


def _parse_market_fields(raw: dict) -> "Market | None":
    """Shared helper: parse raw Gamma API dict into a Market object."""
    try:
        def _parse(field: str, default: str = "[]"):
            v = raw.get(field, default)
            if isinstance(v, str):
                return _json.loads(v)
            return v if v else []

        outcomes  = _parse("outcomes")
        prices    = _parse("outcomePrices")
        token_ids = _parse("clobTokenIds")

        if len(outcomes) < 2 or len(token_ids) < 2:
            return None

        yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("yes", "up")),   0)
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
        logger.debug(f"_parse_market_fields error: {exc}")
        return None


class GammaMixin:
    """Mixin providing Gamma API (public market data) methods."""

    # _get() and config must be provided by the host class (PolymarketClient).

    # ------------------------------------------------------------------
    # Public market queries
    # ------------------------------------------------------------------

    def get_markets(self, limit: int = 200) -> list[Market]:
        """Return active, non-closed markets with sufficient liquidity."""
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
                m = _parse_market_fields(raw)
                if m:
                    markets.append(m)
            except (KeyError, ValueError, TypeError, IndexError) as exc:
                logger.debug(f"Skipping malformed market: {exc}")

        logger.info(f"Fetched {len(markets)} active markets from Gamma API.")
        return markets

    def get_market_by_id(self, market_id: str) -> Market | None:
        """Fetch a single market from the Gamma API by its numeric market ID."""
        data = self._get(f"{config.GAMMA_HOST}/markets/{market_id}")
        if not data:
            return None
        raw = data if isinstance(data, dict) else (data[0] if isinstance(data, list) and data else None)
        if not raw:
            return None
        m = _parse_market_fields(raw)
        if m is None:
            logger.warning(f"get_market_by_id({market_id}) parse error")
        return m

    def get_market_by_clob_token_id(self, token_id: str) -> Market | None:
        """Find a market by its CLOB token ID via the Gamma API."""
        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={"clob_token_ids": _json.dumps([token_id])},
        )
        if not data or not isinstance(data, list):
            return None
        for raw in data:
            try:
                def _parse(field: str, default: str = "[]"):
                    v = raw.get(field, default)
                    if isinstance(v, str):
                        return _json.loads(v)
                    return v if v else []

                tids = _parse("clobTokenIds")
                if token_id not in tids:
                    continue
                m = _parse_market_fields(raw)
                if m:
                    return m
            except Exception as exc:
                logger.debug(f"get_market_by_clob_token_id({token_id[:12]}) parse error: {exc}")
        return None

    def get_updown_markets(self) -> list[Market]:
        """
        Fetch short-interval Up/Down crypto markets using multiple strategies:
        1. Date-range query: markets closing in the next 24 hours
        2. Text search fallback
        3. Soonest-end-date scan
        """
        from src.strategy import _detect_updown_market

        def _parse_updown(raw: dict) -> Market | None:
            question = raw.get("question", "")
            if not _detect_updown_market(question):
                return None
            m = _parse_market_fields(raw)
            if m:
                # Override outcome labels for Up/Down markets
                m.yes_token.outcome = "Up"
                m.no_token.outcome  = "Down"
            return m

        def _collect(data: list | None) -> list[Market]:
            if not data:
                return []
            seen: set[str] = set()
            results: list[Market] = []
            for raw in data:
                m = _parse_updown(raw)
                if m and m.id and m.id not in seen:
                    seen.add(m.id)
                    results.append(m)
            return results

        now     = datetime.now(timezone.utc)
        end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Strategy 1: date-range query — markets closing in the next 24 hours.
        end_max_24h = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={
                "active": "true", "closed": "false", "limit": 500,
                "end_date_min": end_min, "end_date_max": end_max_24h,
            },
        )
        markets = _collect(data)
        if markets:
            logger.info(f"Fetched {len(markets)} Up/Down crypto markets (date-range).")
            return markets

        # Strategy 2: text search
        all_found: list[Market] = []
        seen_ids: set[str] = set()
        for search_term in ("up or down", "5 minute", "15 minute", "1 hour"):
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

        # Strategy 3: soonest end date
        data = self._get(
            f"{config.GAMMA_HOST}/markets",
            params={
                "active": "true", "closed": "false", "limit": 500,
                "order": "endDate", "ascending": "true", "end_date_min": end_min,
            },
        )
        markets = _collect(data)
        if markets:
            logger.info(f"Fetched {len(markets)} Up/Down crypto markets (soonest-first scan).")
            return markets

        logger.debug("No Up/Down crypto markets found — no active slots right now.")
        return []

    def get_price_history(self, market_id: str, fidelity: int = 60) -> list[PricePoint]:
        """Fetch hourly price history for the YES token of a market."""
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
