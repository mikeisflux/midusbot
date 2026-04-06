"""
News Arbitrage Strategy — real-time AP/Reuters/BBC headlines → Polymarket edge.

The edge: major news breaks 10-30 minutes before Polymarket market makers reprice.
This module:
  1. Polls AP News, Reuters, BBC, Politico RSS feeds every 60 seconds
  2. Deduplicates by article GUID — only processes new articles
  3. Keyword pre-filter: finds markets whose question overlaps with article keywords
  4. Sends matched (article, markets) pairs to Claude Opus for probability assessment
  5. Signals when Claude's estimate diverges >NEWS_ARB_EDGE from current market price

Strategy tag: "news_arb"  |  Budget: NEWS_ARB_BUDGET_PCT (15%) of wallet
Requires: ANTHROPIC_API_KEY in .env
"""
from __future__ import annotations

import re
import time
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

import requests
from loguru import logger

# ── Config ────────────────────────────────────────────────────────────────────
HAIKU_MODEL           = "claude-haiku-4-5-20251001"  # Stage 1: cheap semantic screening
OPUS_MODEL            = "claude-opus-4-6"            # Stage 2: probability assessment
NEWS_ARB_EDGE         = 0.12   # minimum probability divergence to fire signal
MIN_LIQUIDITY         = 300    # minimum market liquidity USDC
POLL_INTERVAL         = 60     # poll RSS every 60 seconds
MARKET_CACHE_TTL      = 300    # refresh market list every 5 min
MAX_ARTICLE_AGE_SECS  = 3600   # ignore articles older than 1 hour
HAIKU_BATCH_SIZE      = 25     # markets per Haiku screening call
MAX_ARTICLES_PER_POLL = 5      # max new articles processed per poll cycle

# ── RSS Feed Sources ─────────────────────────────────────────────────────────
# Google News trick: any Google News search URL becomes an RSS feed by inserting /rss/
# Format: https://news.google.com/rss/search?q=site%3A{domain}&hl=en-US&gl=US&ceid=US%3Aen
# This bypasses sites that dropped direct RSS (Reuters, Bloomberg, etc.)
_GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US%3Aen&q="

RSS_SOURCES = [
    # AP News — direct feeds (still active, real-time wire)
    ("AP Top News",       "https://feeds.apnews.com/rss/topnews"),
    ("AP Politics",       "https://feeds.apnews.com/rss/politics"),
    ("AP Business",       "https://feeds.apnews.com/rss/business"),
    ("AP Science",        "https://feeds.apnews.com/rss/science"),
    ("AP Sports",         "https://feeds.apnews.com/rss/sports"),
    # Reuters — via Google News site: operator (Reuters dropped direct RSS)
    ("Reuters Top",       _GN + "site%3Areuters.com"),
    ("Reuters Politics",  _GN + "site%3Areuters.com+politics"),
    ("Reuters Business",  _GN + "site%3Areuters.com+business+finance"),
    ("Reuters Markets",   _GN + "site%3Areuters.com+markets+economy"),
    # BBC — direct feed still active
    ("BBC World",         "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business",      "http://feeds.bbci.co.uk/news/business/rss.xml"),
    # Politico, The Hill — politics/policy
    ("Politico",          "https://rss.politico.com/politics-news.xml"),
    ("The Hill",          "https://thehill.com/rss/syndication/all-news"),
    # Bloomberg via Google News (Bloomberg dropped direct RSS)
    ("Bloomberg",         _GN + "site%3Abloomberg.com"),
    # WSJ via Google News
    ("WSJ",               _GN + "site%3Awsj.com"),
]


@dataclass
class _Article:
    guid:   str
    title:  str
    desc:   str
    source: str
    ts:     float


@dataclass
class NewsArbSignal:
    market_id:      str
    question:       str
    token_id:       str
    side:           str         # "YES" | "NO"
    ai_probability: float
    market_price:   float       # current YES mid price
    edge:           float       # |ai_probability - market_price|
    headline:       str
    reasoning:      str
    source:         str         # e.g. "AP Politics"
    hours_to_close: float
    ts: float = field(default_factory=time.time)


class NewsArbStrategy:
    """
    Singleton. Call maybe_poll() each main loop iteration; it spawns a background
    thread when POLL_INTERVAL has elapsed. Call pop_signals() to consume results.
    """

    def __init__(self, api_key: str, client) -> None:
        self._api_key       = api_key
        self._client        = client
        self._lock          = threading.Lock()
        self._pending:      list[NewsArbSignal] = []
        self._seen:         set[str] = set()        # dedup by article GUID/link
        self._mkt_cache:    list = []
        self._mkt_cache_ts  = 0.0
        self._last_poll     = 0.0
        self._thread:       Optional[threading.Thread] = None
        self.last_headline: str = ""                # shown in dashboard

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def maybe_poll(self) -> None:
        """Start background RSS poll if POLL_INTERVAL has elapsed."""
        if not self.available:
            return
        if time.time() - self._last_poll < POLL_INTERVAL:
            return
        if self._thread and self._thread.is_alive():
            return
        self._last_poll = time.time()
        self._thread = threading.Thread(
            target=self._poll, daemon=True, name="news-arb"
        )
        self._thread.start()

    def pop_signals(self) -> list[NewsArbSignal]:
        with self._lock:
            out = list(self._pending)
            self._pending.clear()
        return out

    # ── Background worker ──────────────────────────────────────────────────────

    def _poll(self) -> None:
        try:
            articles = self._fetch_new()
            if not articles:
                return
            markets = self._get_markets()
            if not markets:
                logger.debug("[NEWS-ARB] No eligible markets — skipping analysis")
                return
            sigs = self._analyze(articles, markets)
            if sigs:
                with self._lock:
                    self._pending.extend(sigs)
        except Exception as exc:
            logger.debug(f"[NEWS-ARB] Poll error: {exc}")

    # ── RSS fetch ──────────────────────────────────────────────────────────────

    def _fetch_new(self) -> list[_Article]:
        cutoff = time.time() - MAX_ARTICLE_AGE_SECS
        new: list[_Article] = []

        for src_name, url in RSS_SOURCES:
            try:
                r = requests.get(
                    url, timeout=8,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; midusbot/1.0)"},
                )
                r.raise_for_status()
                try:
                    root = ET.fromstring(r.content)
                except ET.ParseError:
                    root = ET.fromstring(r.content.decode("utf-8", errors="replace"))
            except Exception as exc:
                logger.debug(f"[NEWS-ARB] {src_name} feed failed: {exc}")
                continue

            count = 0
            for item in root.iter("item"):
                guid = (item.findtext("guid") or item.findtext("link") or "").strip()
                if not guid or guid in self._seen:
                    continue
                title = (item.findtext("title") or "").strip()
                if not title:
                    continue
                ts = _parse_rss_date(item.findtext("pubDate") or "") or time.time()
                if ts < cutoff:
                    self._seen.add(guid)
                    continue
                desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()[:400]
                self._seen.add(guid)
                new.append(_Article(guid=guid, title=title, desc=desc, source=src_name, ts=ts))
                count += 1

            if count:
                logger.debug(f"[NEWS-ARB] {src_name}: {count} new article(s)")

        # Bounded memory — keep only recent 10k GUIDs
        if len(self._seen) > 10_000:
            self._seen = set(list(self._seen)[-5_000:])

        if new:
            logger.info(f"[NEWS-ARB] {len(new)} new article(s) across all feeds")
        return new

    # ── Market cache ───────────────────────────────────────────────────────────

    def _get_markets(self) -> list:
        now = time.time()
        if now - self._mkt_cache_ts < MARKET_CACHE_TTL and self._mkt_cache:
            return self._mkt_cache
        try:
            from src.strategy import _detect_updown_market
            raw      = self._client.get_markets()
            filtered = [
                m for m in raw
                if m.active
                and not m.closed
                and getattr(m, "liquidity", 0) >= MIN_LIQUIDITY
                and 0.03 <= getattr(m, "yes_price", 0.5) <= 0.97
                and getattr(m, "end_date", None)
                # Exclude 5-min UpDown crypto markets — handled by momentum strategy
                and _detect_updown_market(m.question) is None
            ]
            self._mkt_cache    = filtered
            self._mkt_cache_ts = now
            logger.info(f"[NEWS-ARB] Market cache refreshed — {len(filtered)} current-events markets")
        except Exception as exc:
            logger.debug(f"[NEWS-ARB] Market fetch error: {exc}")
        return self._mkt_cache

    # ── Stage 1: Haiku semantic screening ─────────────────────────────────────
    # Haiku screens ALL markets for genuine relevance — no keyword shortcuts.
    # Runs in batches of HAIKU_BATCH_SIZE. Cost: ~$0.003 per article screened.

    def _haiku_screen(self, article: _Article, markets: list, client) -> list:
        """
        Ask Claude Haiku whether each market is CAUSALLY relevant to the article.
        Returns only markets where Haiku is confident the news directly shifts odds.

        Haiku is strict: entity overlap alone is rejected. 'Trump signs bill' does
        NOT match 'Will Iran attack Israel?' unless the bill directly concerns Iran.
        """
        relevant: list = []

        for i in range(0, len(markets), HAIKU_BATCH_SIZE):
            batch = markets[i : i + HAIKU_BATCH_SIZE]
            mkt_block = "\n".join(
                f"  {m.id[:16]} | {m.question[:90]}"
                for m in batch
            )
            prompt = (
                "A breaking news article was just published. Your job: identify which "
                "Polymarket prediction markets (if any) this article is CAUSALLY and "
                "DIRECTLY relevant to — meaning the news genuinely changes the probability "
                "of the market's outcome.\n\n"
                f"HEADLINE: {article.title}\n"
                f"SOURCE: {article.source}\n"
                f"DETAILS: {article.desc[:300]}\n\n"
                "MARKETS TO SCREEN (ID | question):\n"
                f"{mkt_block}\n\n"
                "STRICT RULES — a market is relevant ONLY if:\n"
                "  1. The article is about the EXACT same subject as the market question\n"
                "  2. The news outcome would DIRECTLY change the resolution probability\n"
                "  3. There is a clear causal chain, not just shared keywords\n\n"
                "COUNTER-EXAMPLES (do NOT match these):\n"
                "  - 'Trump signs budget bill' ≠ 'Will Iran attack Israel?' (no causal link)\n"
                "  - 'Bitcoin price rises' ≠ 'Will Ethereum hit $5k?' (different asset)\n"
                "  - 'Fed official speaks' ≠ 'Will Fed cut rates?' (unless it's a rate decision)\n\n"
                "Reply with ONLY the relevant market IDs, one per line. "
                "If none are relevant, reply: NONE"
            )

            try:
                msg = client.messages.create(
                    model=HAIKU_MODEL, max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                )
                response = msg.content[0].text.strip()
            except Exception as exc:
                logger.debug(f"[NEWS-ARB] Haiku screen error: {exc}")
                continue

            if response.strip().upper() == "NONE" or not response.strip():
                continue

            for line in response.splitlines():
                prefix = line.strip().split()[0] if line.strip() else ""
                if not prefix:
                    continue
                match = next(
                    (m for m in batch
                     if m.id.startswith(prefix) or prefix.startswith(m.id[:8])),
                    None,
                )
                if match and match not in relevant:
                    relevant.append(match)

        if relevant:
            logger.info(
                f"[NEWS-ARB] Haiku screened {len(markets)} markets → "
                f"{len(relevant)} causally relevant for '{article.title[:55]}'"
            )
        return relevant

    # ── Stage 2: Opus probability assessment ───────────────────────────────────
    # Only runs on Haiku-confirmed relevant markets. Gives precise probability.

    def _opus_assess(
        self,
        article: _Article,
        markets: list,
        client,
    ) -> list[NewsArbSignal]:
        """Ask Claude Opus for precise probability estimates on confirmed-relevant markets."""
        mkt_block = "\n".join(
            f"  {m.id[:16]} | YES@{m.yes_price:.3f} | {m.question[:80]}"
            for m in markets
        )
        prompt = (
            "You are a Polymarket probability analyst. The following breaking news has just "
            "been confirmed as directly relevant to these markets:\n\n"
            f"HEADLINE: {article.title}\n"
            f"SOURCE: {article.source}\n"
            f"DETAILS: {article.desc[:400]}\n\n"
            "MARKETS (ID | current YES price | question):\n"
            f"{mkt_block}\n\n"
            "For each market, give your updated YES probability given this news.\n"
            "Format exactly: ID_PREFIX | NEW_YES_PROB | one-sentence reasoning\n\n"
            "Rules:\n"
            "- NEW_YES_PROB must be 0.00–1.00\n"
            "- Only output a line if the news moves the probability by >12 percentage points\n"
            "- Account for: how definitive the news is, source reliability, reversibility\n"
            "- If no market crosses the 12-point threshold, reply: NO_SIGNAL"
        )

        try:
            msg = client.messages.create(
                model=OPUS_MODEL, max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            response = msg.content[0].text.strip()
        except Exception as exc:
            logger.debug(f"[NEWS-ARB] Opus assess error: {exc}")
            return []

        if "NO_SIGNAL" in response:
            logger.debug(f"[NEWS-ARB] Opus: insufficient edge on '{article.title[:50]}'")
            return []

        signals: list[NewsArbSignal] = []
        for line in response.splitlines():
            line = line.strip()
            if not line or line.count("|") < 2:
                continue
            parts = [p.strip() for p in line.split("|")]
            try:
                id_prefix = parts[0]
                ai_prob   = float(parts[1])
                reasoning = parts[2]
            except (ValueError, IndexError):
                continue
            if not (0.0 < ai_prob < 1.0):
                continue

            market = next(
                (m for m in markets
                 if m.id.startswith(id_prefix) or id_prefix.startswith(m.id[:8])),
                None,
            )
            if not market:
                continue

            mid  = market.yes_price
            edge = abs(ai_prob - mid)
            if edge < NEWS_ARB_EDGE:
                continue

            direction = "YES" if ai_prob > mid else "NO"
            token_id  = (
                market.yes_token.token_id if direction == "YES"
                else market.no_token.token_id
            )
            hrs = _hours_to_close(market)
            if hrs <= 0:
                continue

            self.last_headline = article.title
            logger.info(
                f"[NEWS-ARB] SIGNAL {direction} Opus={ai_prob:.2f} mkt={mid:.2f} "
                f"edge={edge:.1%} '{market.question[:55]}' | {reasoning[:60]}"
            )
            signals.append(NewsArbSignal(
                market_id=market.id,
                question=market.question,
                token_id=token_id,
                side=direction,
                ai_probability=ai_prob,
                market_price=mid,
                edge=edge,
                headline=article.title,
                reasoning=reasoning,
                source=article.source,
                hours_to_close=hrs,
            ))
        return signals

    # ── Orchestrator ───────────────────────────────────────────────────────────

    def _analyze(self, articles: list[_Article], markets: list) -> list[NewsArbSignal]:
        """
        Two-stage pipeline:
          1. Haiku screens all markets for genuine causal relevance (cheap, fast)
          2. Opus assesses probability only on confirmed matches (accurate, expensive)
        """
        import anthropic
        api_client = anthropic.Anthropic(api_key=self._api_key)
        signals: list[NewsArbSignal] = []

        for art in articles[:MAX_ARTICLES_PER_POLL]:
            logger.debug(f"[NEWS-ARB] Screening: '{art.title[:65]}' ({art.source})")

            # Stage 1 — Haiku semantic screening across all markets
            confirmed = self._haiku_screen(art, markets, api_client)
            if not confirmed:
                continue

            # Stage 2 — Opus probability assessment on confirmed markets only
            art_signals = self._opus_assess(art, confirmed, api_client)
            signals.extend(art_signals)

        return signals


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_rss_date(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    # ISO fallback
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _hours_to_close(market) -> float:
    end_date = getattr(market, "end_date", None)
    if not end_date:
        return 24.0
    try:
        from datetime import datetime, timezone
        end  = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        secs = (end - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, secs / 3600)
    except Exception:
        return 24.0
