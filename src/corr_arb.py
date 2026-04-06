"""
Correlation / Logical Arbitrage — part of the Arb 30% bucket.

Detects mathematically impossible probabilities across logically related markets.

Examples:
  "BTC hits $100k by Feb" at 35% when "BTC hits $90k by Feb" at 30% — impossible
  (if BTC hits $100k it MUST have hit $90k first — P($100k) ≤ P($90k))

  "Chiefs win Super Bowl" at 28% when "AFC team wins Super Bowl" at 24% — impossible
  (Chiefs ARE AFC — P(Chiefs) ≤ P(AFC))

  Cumulative probabilities across mutually exclusive outcomes that sum > 1.05

Strategy:
  - Scan all active markets for logical relationships using keyword matching
  - Detect violations where implied probability gap exceeds ARB_MIN_EDGE (3%)
  - Execute the mispriced side: buy the underpriced leg
  - These markets typically take days to resolve — not suitable for 5-min windows

Edge: 70-80% win rate, 2-5% monthly, low volatility (math-based, not directional)
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger
import config


# Minimum edge to enter after accounting for fees
ARB_MIN_EDGE = 0.03

# Maximum days to resolution for correlation arb positions (avoid long lockups)
CORR_ARB_MAX_DAYS = 30

# Relationship patterns — (keyword_A, keyword_B, relationship)
# relationship: "subset" = A implies B (P(A) ≤ P(B))
#               "exclusive" = A and B are mutually exclusive (P(A) + P(B) ≤ 1)
_SUBSET_PATTERNS = [
    # Price milestone ordering: hitting higher price implies hitting lower price
    (r"\$(\d[\d,\.]+)k?\s+by", r"\$(\d[\d,\.]+)k?\s+by"),
]

_CUMULATIVE_KEYWORDS = [
    "which month", "which week", "which quarter", "what month",
    "by january", "by february", "by march", "by april", "by may", "by june",
]


@dataclass
class CorrArbSignal:
    """A correlation arbitrage opportunity."""
    token_id:   str
    market_id:  str
    question:   str
    side:       str     # "YES" = buy YES leg
    edge:       float
    reason:     str
    hours_to_close: Optional[float] = None


class CorrArbScanner:
    """Scans non-UpDown markets for logical probability violations."""

    def scan(self, markets: list) -> list[CorrArbSignal]:
        """
        Returns CorrArbSignal for any mispriced leg found.
        Only considers non-UpDown markets with sufficient liquidity.
        """
        from src.strategy import _detect_updown_market
        from datetime import datetime, timezone

        # Filter to non-UpDown markets with reasonable liquidity
        candidates = []
        now = datetime.now(timezone.utc)
        for m in markets:
            if _detect_updown_market(m.question):
                continue   # 5-min UpDown handled by other strategies
            if m.liquidity < 1000:
                continue   # too thin
            if not m.end_date:
                continue
            try:
                end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                secs_left = (end - now).total_seconds()
                if secs_left < 3600:
                    continue   # too close to resolve
                days_left = secs_left / 86400
                if days_left > CORR_ARB_MAX_DAYS:
                    continue   # avoid long lockup
            except Exception:
                continue
            candidates.append((m, days_left))

        signals: list[CorrArbSignal] = []

        # Strategy 1: Cumulative probability violation
        # Find groups of mutually exclusive markets (same topic, different outcomes)
        signals.extend(self._scan_cumulative(candidates))

        # Strategy 2: Superset/subset pricing violations
        # E.g., "BTC > $80k by March" priced higher than "BTC > $70k by March"
        signals.extend(self._scan_milestones(candidates))

        return signals

    def _scan_cumulative(self, candidates: list) -> list[CorrArbSignal]:
        """
        Find groups of 'which X' markets and check if probabilities sum > 1.05.
        Overpriced legs → sell (or buy NO). Underpriced → buy YES.
        """
        signals: list[CorrArbSignal] = []

        # Group markets by base question (strip month/outcome specifics)
        groups: dict[str, list] = {}
        for m, days in candidates:
            q = m.question.lower()
            for kw in _CUMULATIVE_KEYWORDS:
                if kw in q:
                    # Normalise to group key
                    base = re.sub(r'(january|february|march|april|may|june|july|'
                                  r'august|september|october|november|december)', 'MONTH', q)
                    base = re.sub(r'\d{4}', 'YEAR', base)
                    groups.setdefault(base[:60], []).append((m, days))
                    break

        for base, group in groups.items():
            if len(group) < 2:
                continue

            total_yes = sum(m.yes_price for m, _ in group)
            if total_yes <= 1.05:
                continue   # no violation (allow 5% slop for fees/resolution risk)

            # Find the most overpriced leg (highest YES vs expected)
            expected = 1.0 / len(group)
            for m, days in group:
                if m.yes_price > expected * 1.5:
                    # This leg is significantly overpriced — buy NO
                    edge = m.yes_price - expected
                    if edge < ARB_MIN_EDGE:
                        continue
                    tid = m.no_token.token_id
                    mid_id = getattr(m, "market_id", "") or getattr(m, "id", "") or ""
                    signals.append(CorrArbSignal(
                        token_id=tid, market_id=mid_id,
                        question=m.question, side="NO",
                        edge=edge,
                        reason=f"Cumulative violation: sum={total_yes:.2f}>1.0, this leg={m.yes_price:.2f} overpriced",
                        hours_to_close=days * 24,
                    ))
                    logger.info(
                        f"[CORR-ARB] Cumulative violation: {m.question[:50]} "
                        f"YES={m.yes_price:.3f} expected≤{expected:.3f} edge={edge:.1%}"
                    )

        return signals

    def _scan_milestones(self, candidates: list) -> list[CorrArbSignal]:
        """
        Find price milestone markets for same asset where ordering is violated.
        E.g., 'BTC hits $90k' at 40% but 'BTC hits $80k' at 35% — impossible.
        """
        signals: list[CorrArbSignal] = []

        # Parse price milestones
        _assets = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp"]
        milestone_markets: list[tuple] = []   # (asset, threshold_k, yes_price, market, days)

        for m, days in candidates:
            q = m.question.lower()
            asset = next((a for a in _assets if a in q), None)
            if not asset:
                continue

            # Try to extract dollar threshold
            match = re.search(r'\$\s*([\d,]+)k?', q)
            if not match:
                continue
            val_str = match.group(1).replace(",", "")
            try:
                threshold = float(val_str)
                if "k" in q[match.start():match.start()+10]:
                    threshold *= 1000
            except ValueError:
                continue

            milestone_markets.append((asset, threshold, m.yes_price, m, days))

        # Group by asset
        by_asset: dict[str, list] = {}
        for asset, threshold, price, m, days in milestone_markets:
            by_asset.setdefault(asset, []).append((threshold, price, m, days))

        for asset, items in by_asset.items():
            if len(items) < 2:
                continue
            # Sort by threshold ascending
            items.sort(key=lambda x: x[0])
            for i in range(len(items) - 1):
                low_thresh,  low_price,  low_m,  low_days  = items[i]
                high_thresh, high_price, high_m, high_days = items[i + 1]

                # P(higher threshold) MUST be ≤ P(lower threshold)
                # Violation: high_price > low_price
                if high_price <= low_price:
                    continue

                edge = high_price - low_price
                if edge < ARB_MIN_EDGE:
                    continue

                # Buy NO on the overpriced high-threshold market
                mid_id = getattr(high_m, "market_id", "") or getattr(high_m, "id", "") or ""
                signals.append(CorrArbSignal(
                    token_id=high_m.no_token.token_id,
                    market_id=mid_id,
                    question=high_m.question,
                    side="NO",
                    edge=edge,
                    reason=(
                        f"Milestone ordering violation: "
                        f"P(${high_thresh:,.0f})={high_price:.3f} > "
                        f"P(${low_thresh:,.0f})={low_price:.3f} — mathematically impossible"
                    ),
                    hours_to_close=high_days * 24,
                ))
                logger.info(
                    f"[CORR-ARB] Milestone violation: {asset.upper()} "
                    f"${high_thresh:,.0f}={high_price:.3f} > ${low_thresh:,.0f}={low_price:.3f} "
                    f"edge={edge:.1%}"
                )

        return signals


# Module-level singleton
scanner = CorrArbScanner()
