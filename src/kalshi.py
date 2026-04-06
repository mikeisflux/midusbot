"""
Kalshi Cross-Platform Arbitrage — part of the Arb 30% bucket.

Compares Polymarket vs Kalshi prices for the same underlying event.
When one platform prices an outcome significantly cheaper than the other,
buy the cheap side and hedge/hold for convergence.

Examples:
  "Will BTC be above $80k at end of March?" — Polymarket: 35%, Kalshi: 28%
  → Buy Kalshi YES (cheaper) since same event must resolve identically.

API:
  Kalshi REST API v2 — public market data requires no auth for reads.
  Base URL: https://trading-api.kalshi.com/trade-api/v2

Strategy:
  1. Fetch Kalshi markets for crypto/macro topics overlapping with Polymarket
  2. Match by question similarity (keyword matching, not ML)
  3. Signal when |poly_price - kalshi_price| > KALSHI_MIN_EDGE (8%)
  4. In live mode: buy the cheaper platform's side
  5. Track as strategy="arb"

Notes:
  - Kalshi denominations: prices in cents (0-99), normalize to 0.0-1.0
  - Kalshi API rate limit: 10 req/s public, 100 req/min
  - This is read-only — actual Kalshi trading requires a separate Kalshi account
    and API key. Signals are logged but only Polymarket leg is executed.
  - Win condition: Polymarket and Kalshi converge (they must for same event)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

import requests
from loguru import logger


KALSHI_MIN_EDGE     = 0.08     # minimum price divergence to signal
KALSHI_MAX_DAYS     = 14       # only compare markets resolving within 14 days
KALSHI_POLL_INTERVAL = 120     # seconds between Kalshi API calls
_KALSHI_BASE        = "https://trading-api.kalshi.com/trade-api/v2"

# Keywords to match Polymarket questions against Kalshi events
_CRYPTO_KEYWORDS = ["bitcoin", "btc", "ethereum", "eth", "crypto", "xrp", "solana", "sol"]
_MACRO_KEYWORDS  = ["fed", "cpi", "inflation", "rate", "gdp", "recession"]

_last_poll: float = 0.0


@dataclass
class KalshiSignal:
    """A Kalshi vs Polymarket arbitrage opportunity."""
    poly_token_id: str
    poly_market_id: str
    poly_question: str
    poly_price: float          # YES price on Polymarket
    kalshi_market_id: str
    kalshi_question: str
    kalshi_price: float        # YES price on Kalshi (normalized 0-1)
    edge: float                # |poly_price - kalshi_price|
    cheap_side: str            # "POLY" = buy Polymarket, "KALSHI" = buy Kalshi
    side: str                  # "YES" or "NO" to buy on Polymarket
    hours_to_close: float


class KalshiScanner:
    """
    Fetches Kalshi public markets and compares against Polymarket candidates.
    Call scan() from _run_corr_arb loop or a dedicated scanner hook.
    """

    def __init__(self):
        self._kalshi_cache: list[dict] = []
        self._cache_ts: float = 0.0

    def _fetch_kalshi_markets(self) -> list[dict]:
        """Fetch Kalshi active markets. Cached for KALSHI_POLL_INTERVAL seconds."""
        now = time.time()
        if now - self._cache_ts < KALSHI_POLL_INTERVAL and self._kalshi_cache:
            return self._kalshi_cache

        try:
            resp = requests.get(
                f"{_KALSHI_BASE}/markets",
                params={
                    "status":  "open",
                    "limit":   200,
                    "series_ticker": None,
                },
                timeout=10,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            markets = resp.json().get("markets") or []
            self._kalshi_cache = markets
            self._cache_ts = now
            logger.debug(f"[KALSHI] Fetched {len(markets)} open markets")
            return markets
        except Exception as exc:
            logger.debug(f"[KALSHI] API fetch failed: {exc}")
            return self._kalshi_cache   # return stale if available

    def _normalize_question(self, text: str) -> str:
        """Lowercase, strip punctuation, reduce to keywords for fuzzy matching."""
        t = text.lower()
        t = re.sub(r"[^a-z0-9$ ]", " ", t)
        return " ".join(t.split())

    def _questions_match(self, poly_q: str, kalshi_q: str) -> bool:
        """
        Returns True if two questions are likely about the same underlying event.
        Uses keyword overlap — good enough for crypto/macro topics.
        """
        pq = set(self._normalize_question(poly_q).split())
        kq = set(self._normalize_question(kalshi_q).split())

        # Must share enough meaningful words
        stopwords = {"will", "be", "the", "a", "an", "in", "of", "at", "by",
                     "is", "to", "for", "on", "end", "does", "do", "hit", "reach"}
        pq -= stopwords
        kq -= stopwords
        if len(pq) < 2 or len(kq) < 2:
            return False

        overlap = pq & kq
        jaccard  = len(overlap) / len(pq | kq)
        return jaccard >= 0.40   # 40% word overlap = same topic

    def scan(self, poly_markets: list) -> list[KalshiSignal]:
        """
        Compare Polymarket candidates against Kalshi markets.
        Returns KalshiSignal for any divergent pricing found.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        kalshi_markets = self._fetch_kalshi_markets()
        if not kalshi_markets:
            return []

        signals: list[KalshiSignal] = []

        for pm in poly_markets:
            # Only consider non-UpDown markets with at least some liquidity
            from src.strategy import _detect_updown_market
            if _detect_updown_market(pm.question):
                continue
            if pm.liquidity < 500:
                continue
            if not pm.end_date:
                continue

            try:
                end_dt = datetime.fromisoformat(pm.end_date.replace("Z", "+00:00"))
                days_left = (end_dt - now).total_seconds() / 86400
                if days_left < 0 or days_left > KALSHI_MAX_DAYS:
                    continue
                hours_to_close = days_left * 24
            except Exception:
                continue

            poly_price = pm.yes_price
            if not (0.05 <= poly_price <= 0.95):
                continue

            # Check keywords
            q_lower = pm.question.lower()
            has_keyword = any(kw in q_lower for kw in _CRYPTO_KEYWORDS + _MACRO_KEYWORDS)
            if not has_keyword:
                continue

            # Find matching Kalshi market
            for km in kalshi_markets:
                km_title = km.get("title", "") or km.get("question", "") or ""
                if not km_title:
                    continue

                if not self._questions_match(pm.question, km_title):
                    continue

                # Kalshi prices YES in cents → normalize to 0-1
                yes_cents = km.get("yes_ask") or km.get("yes_bid") or 0
                kalshi_price = yes_cents / 100.0 if yes_cents else 0.0
                if not (0.05 <= kalshi_price <= 0.95):
                    continue

                edge = abs(poly_price - kalshi_price)
                if edge < KALSHI_MIN_EDGE:
                    continue

                # Determine which side is cheaper
                pm_id = getattr(pm, "market_id", "") or getattr(pm, "id", "") or ""
                if poly_price < kalshi_price:
                    # Polymarket is cheaper → buy Polymarket YES
                    cheap_side = "POLY"
                    side = "YES"
                    tok = pm.yes_token.token_id
                else:
                    # Kalshi is cheaper → Polymarket is overpriced → buy Polymarket NO
                    cheap_side = "KALSHI"
                    side = "NO"
                    tok = pm.no_token.token_id

                signals.append(KalshiSignal(
                    poly_token_id=tok,
                    poly_market_id=pm_id,
                    poly_question=pm.question,
                    poly_price=poly_price,
                    kalshi_market_id=km.get("ticker", ""),
                    kalshi_question=km_title,
                    kalshi_price=kalshi_price,
                    edge=edge,
                    cheap_side=cheap_side,
                    side=side,
                    hours_to_close=hours_to_close,
                ))
                logger.info(
                    f"[KALSHI-ARB] {side} edge={edge:.1%}  "
                    f"poly={poly_price:.3f} kalshi={kalshi_price:.3f}  "
                    f"{pm.question[:50]}"
                )
                break   # one Kalshi match per Poly market is enough

        return signals


# Module-level singleton
scanner = KalshiScanner()
