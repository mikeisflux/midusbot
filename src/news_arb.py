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
CLAUDE_MODEL          = "claude-opus-4-6"
NEWS_ARB_EDGE         = 0.12   # minimum probability divergence to fire signal
MIN_LIQUIDITY         = 300    # minimum market liquidity USDC
POLL_INTERVAL         = 60     # poll RSS every 60 seconds
MARKET_CACHE_TTL      = 300    # refresh market list every 5 min
MAX_ARTICLE_AGE_SECS  = 3600   # ignore articles older than 1 hour
MAX_MARKETS_PER_QUERY = 8      # max markets sent to Claude per article
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

_STOPWORDS = {
    "will", "that", "this", "with", "from", "have", "been", "they", "their",
    "what", "when", "where", "which", "would", "could", "should", "about",
    "after", "before", "during", "while", "says", "said", "more", "than",
    "into", "over", "also", "year", "years", "some", "time", "make", "made",
    "both", "each", "just", "does", "doing", "were", "only", "then", "them",
    "these", "those", "such", "much", "very", "well", "back", "even", "here",
    "there", "being", "going", "getting", "having", "looking", "coming",
}


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

    # ── Keyword pre-filter ─────────────────────────────────────────────────────

    @staticmethod
    def _keywords(text: str) -> list[str]:
        words = re.findall(r"\b[A-Za-z]{4,}\b", text.lower())
        return [w for w in words if w not in _STOPWORDS]

    def _match_markets(self, article: _Article, markets: list) -> list:
        kws = self._keywords(article.title + " " + article.desc[:200])
        if len(kws) < 2:
            return []
        scored = []
        for m in markets:
            q_lower = m.question.lower()
            score   = sum(1 for kw in kws if kw in q_lower)
            if score >= 2:
                scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:MAX_MARKETS_PER_QUERY]]

    # ── Claude analysis ────────────────────────────────────────────────────────

    def _analyze(self, articles: list[_Article], markets: list) -> list[NewsArbSignal]:
        import anthropic
        api_client = anthropic.Anthropic(api_key=self._api_key)
        signals: list[NewsArbSignal] = []

        for art in articles[:MAX_ARTICLES_PER_POLL]:
            matched = self._match_markets(art, markets)
            if not matched:
                continue

            logger.info(
                f"[NEWS-ARB] '{art.title[:65]}' ({art.source}) "
                f"→ {len(matched)} keyword-matched market(s)"
            )

            mkt_block = "\n".join(
                f"  {m.id[:16]} | YES@{m.yes_price:.3f} | {m.question[:80]}"
                for m in matched
            )

            prompt = (
                "You are a Polymarket probability analyst. Breaking news just published:\n\n"
                f"HEADLINE: {art.title}\n"
                f"SOURCE: {art.source}\n"
                f"DETAILS: {art.desc[:350]}\n\n"
                "RELATED POLYMARKET MARKETS (16-char ID prefix | current YES price | question):\n"
                f"{mkt_block}\n\n"
                "TASK: For each market where this news SIGNIFICANTLY changes the probability, respond:\n"
                "  ID_PREFIX | YOUR_YES_PROBABILITY | one-sentence reason\n\n"
                "Rules:\n"
                "- Only include markets where news shifts probability by >12 percentage points\n"
                "- YOUR_YES_PROBABILITY: your estimate of true YES probability (0.00–1.00)\n"
                "- Be conservative — only signal when news is DIRECTLY, UNAMBIGUOUSLY relevant\n"
                "- Breaking news moving fast: weight recency heavily\n"
                "- If nothing is significantly affected, reply exactly: NO_SIGNAL"
            )

            try:
                msg = api_client.messages.create(
                    model=CLAUDE_MODEL, max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                )
                response = msg.content[0].text.strip()
            except Exception as exc:
                logger.debug(f"[NEWS-ARB] Claude API error: {exc}")
                continue

            if "NO_SIGNAL" in response:
                logger.debug(f"[NEWS-ARB] No edge on: '{art.title[:55]}'")
                continue

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
                    (m for m in matched
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
                    continue  # already expired

                self.last_headline = art.title
                logger.info(
                    f"[NEWS-ARB] SIGNAL {direction} "
                    f"Claude={ai_prob:.2f} mkt={mid:.2f} edge={edge:.1%} "
                    f"'{market.question[:55]}' | {reasoning[:60]}"
                )
                signals.append(NewsArbSignal(
                    market_id=market.id,
                    question=market.question,
                    token_id=token_id,
                    side=direction,
                    ai_probability=ai_prob,
                    market_price=mid,
                    edge=edge,
                    headline=art.title,
                    reasoning=reasoning,
                    source=art.source,
                    hours_to_close=hrs,
                ))

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
