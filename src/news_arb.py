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
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import requests
from loguru import logger

# ── Config ────────────────────────────────────────────────────────────────────
HAIKU_MODEL           = "claude-haiku-4-5-20251001"  # Stage 1: cheap semantic screening
OPUS_MODEL            = "claude-opus-4-6"            # Stage 2: probability assessment
NEWS_ARB_EDGE         = 0.08   # minimum probability divergence to fire signal (was 0.12 — too strict)
MIN_LIQUIDITY         = 100    # minimum market liquidity USDC (was 300 — too restrictive)
POLL_INTERVAL         = 15     # poll RSS every 15 seconds
MARKET_CACHE_TTL      = 300    # refresh market list every 5 min
MAX_ARTICLE_AGE_SECS  = 600    # ignore articles older than 10 minutes — freshness required
HAIKU_BATCH_SIZE      = 25     # markets per Haiku screening call
MAX_ARTICLES_PER_POLL = 10     # max new articles processed per poll cycle (was 5)

# ── Price velocity (smart money) parameters ───────────────────────────────────
VELOCITY_POLL_SECS     = 15    # check order book prices every 15 seconds
VELOCITY_WINDOW_SECS   = 600   # detect moves within this rolling window (10 min)
VELOCITY_MIN_MOVE      = 0.08  # minimum absolute price shift to investigate (8pp)
VELOCITY_STABLE_SECS   = 1800  # market must have been flat this long before signal (30 min)
VELOCITY_STABLE_MAX    = 0.025 # max allowed drift during the stable window (2.5pp)
VELOCITY_COOLDOWN_SECS = 900   # suppress repeated signals on same market (15 min)

# ── Title deduplication helpers ───────────────────────────────────────────────
# Prevents follow-up / "extended version" articles on the same topic from
# triggering a second position. Uses Jaccard similarity on content words.
_STOP_WORDS = frozenset({
    "the","a","an","in","on","at","to","for","of","and","or","but","is","are",
    "was","were","be","been","being","have","has","had","will","would","could",
    "should","may","might","with","from","by","up","as","his","her","its",
    "their","this","that","these","those","new","says","said","say","after",
    "over","into","than","more","also","about","how","who","what","when",
    "update","breaking","analysis","exclusive","report","reports","sources",
})

def _title_tokens(title: str) -> frozenset:
    """Return meaningful content words from a headline for similarity comparison."""
    words = re.sub(r"[^a-z0-9\s]", "", title.lower()).split()
    return frozenset(w for w in words if len(w) > 3 and w not in _STOP_WORDS)


# ── RSS Feed Sources ─────────────────────────────────────────────────────────
# Google News trick: any Google News search URL becomes an RSS feed by inserting /rss/
# Format: https://news.google.com/rss/search?q=site%3A{domain}&hl=en-US&gl=US&ceid=US%3Aen
# This bypasses sites that dropped direct RSS (Reuters, Bloomberg, etc.)
_GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US%3Aen&q="

RSS_SOURCES = [
    # AP News — via Google News (direct feeds.apnews.com may fail DNS on some servers)
    ("AP Top News",       _GN + "site%3Aapnews.com"),
    ("AP Politics",       _GN + "site%3Aapnews.com+politics"),
    ("AP Business",       _GN + "site%3Aapnews.com+business"),
    ("AP World",          _GN + "site%3Aapnews.com+world"),
    # Reuters — via Google News site: operator (Reuters dropped direct RSS)
    ("Reuters Top",       _GN + "site%3Areuters.com"),
    ("Reuters Politics",  _GN + "site%3Areuters.com+politics"),
    ("Reuters Business",  _GN + "site%3Areuters.com+business+finance"),
    ("Reuters Markets",   _GN + "site%3Areuters.com+markets+economy"),
    # BBC — direct feed still active
    ("BBC World",         "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business",      "http://feeds.bbci.co.uk/news/business/rss.xml"),
    # Politico — politics/policy
    ("Politico",          "https://rss.politico.com/politics-news.xml"),
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
        # Price velocity state — tracks YES prices per market for smart-money detection
        # market_id → deque of (timestamp, yes_price)
        self._price_history: dict[str, deque] = {}
        self._velocity_last_check = 0.0
        # market_id → last time we fired a velocity signal (cooldown)
        self._velocity_fired: dict[str, float] = {}
        self._vel_thread: Optional[threading.Thread] = None
        # Topic dedup: (timestamp, token_set) for articles we actually analyzed today
        self._analyzed_titles: list[tuple[float, frozenset]] = []

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _is_topic_repeat(self, title: str) -> bool:
        """True if this headline is a follow-up / extended version of a story already analyzed today."""
        tokens = _title_tokens(title)
        if not tokens:
            return False
        cutoff = time.time() - 86400  # rolling 24h window
        for ts, prev in self._analyzed_titles:
            if ts < cutoff:
                continue
            union = tokens | prev
            if not union:
                continue
            jaccard = len(tokens & prev) / len(union)
            if jaccard >= 0.45:  # 45% word overlap = same story
                return True
        return False

    def _record_analyzed(self, title: str) -> None:
        """Mark a headline as analyzed so follow-ups are suppressed."""
        now = time.time()
        self._analyzed_titles.append((now, _title_tokens(title)))
        # Prune entries older than 24h
        cutoff = now - 86400
        self._analyzed_titles = [(ts, t) for ts, t in self._analyzed_titles if ts > cutoff]

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

    def maybe_check_velocity(self) -> None:
        """
        Detect Polymarket smart-money moves BEFORE news hits RSS.
        Informed traders (Bloomberg Terminal, etc.) buy 15-30 min before articles
        reach RSS feeds. A market that has been flat for 30 min then suddenly moves
        8+ percentage points is almost certainly being front-run.
        Runs every VELOCITY_POLL_SECS seconds in a separate background thread.
        """
        if not self.available:
            return
        if time.time() - self._velocity_last_check < VELOCITY_POLL_SECS:
            return
        if self._vel_thread and self._vel_thread.is_alive():
            return
        self._velocity_last_check = time.time()
        self._vel_thread = threading.Thread(
            target=self._velocity_scan, daemon=True, name="news-velocity"
        )
        self._vel_thread.start()

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
                    proxies={"http": None, "https": None},  # bypass system proxy
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
            # Skip follow-up / "extended version" articles — same topic already analyzed today
            if self._is_topic_repeat(art.title):
                logger.debug(
                    f"[NEWS-ARB] Topic repeat — skipping follow-up article: '{art.title[:65]}'"
                )
                continue

            logger.info(f"[NEWS-ARB] Screening: '{art.title[:65]}' ({art.source})")
            self._record_analyzed(art.title)  # mark BEFORE calling Haiku (even if no match)

            # Stage 1 — Haiku semantic screening across all markets
            confirmed = self._haiku_screen(art, markets, api_client)
            if not confirmed:
                logger.debug(f"[NEWS-ARB] Haiku: no relevant markets for '{art.title[:55]}'")
                continue

            # Stage 2 — Opus probability assessment on confirmed markets only
            art_signals = self._opus_assess(art, confirmed, api_client)
            if not art_signals:
                logger.info(f"[NEWS-ARB] Opus: edge too small on '{art.title[:55]}'")
            signals.extend(art_signals)

        if not signals and articles:
            logger.info(f"[NEWS-ARB] {len(articles)} article(s) processed — no signals above {NEWS_ARB_EDGE:.0%} edge")
        return signals


# ── Price velocity scanner ─────────────────────────────────────────────────────

    def _velocity_scan(self) -> None:
        """
        Background worker: snapshot YES prices for all current-events markets,
        then look for 'stable → sudden move' patterns that signal informed buying.
        """
        try:
            now     = time.time()
            markets = self._get_markets()
            if not markets:
                return

            movers: list[tuple] = []  # (market, old_price, new_price, direction)

            for mkt in markets:
                mid = getattr(mkt, "yes_price", None)
                if mid is None or not (0.03 <= mid <= 0.97):
                    continue

                hist = self._price_history.setdefault(
                    mkt.id, deque(maxlen=int(VELOCITY_STABLE_SECS / VELOCITY_POLL_SECS) + 10)
                )
                hist.append((now, mid))

                # Need enough history to assess stability (≥ VELOCITY_STABLE_SECS of data)
                if not hist or (now - hist[0][0]) < VELOCITY_STABLE_SECS * 0.8:
                    continue

                # Split history into: stable window (older) + move window (recent)
                move_cutoff   = now - VELOCITY_WINDOW_SECS
                stable_cutoff = now - VELOCITY_STABLE_SECS

                stable_prices = [p for t, p in hist if stable_cutoff <= t < move_cutoff]
                recent_prices = [p for t, p in hist if t >= move_cutoff]

                if len(stable_prices) < 5 or len(recent_prices) < 3:
                    continue

                stable_range = max(stable_prices) - min(stable_prices)
                if stable_range > VELOCITY_STABLE_MAX:
                    # Market was already volatile — not a clean setup
                    continue

                stable_mid = sum(stable_prices) / len(stable_prices)
                current    = recent_prices[-1]
                move       = current - stable_mid  # positive = moving YES-ward

                if abs(move) < VELOCITY_MIN_MOVE:
                    continue

                # Cooldown: suppress re-firing on same market within 15 min
                last_fired = self._velocity_fired.get(mkt.id, 0.0)
                if now - last_fired < VELOCITY_COOLDOWN_SECS:
                    continue

                direction = "YES" if move > 0 else "NO"
                movers.append((mkt, stable_mid, current, move, direction))
                logger.info(
                    f"[VELOCITY] {mkt.question[:60]} | "
                    f"stable={stable_mid:.3f} → now={current:.3f} ({move:+.3f}) "
                    f"over last {VELOCITY_WINDOW_SECS//60}min"
                )

            if not movers:
                return

            # Ask Haiku whether each move looks like informed trading
            import anthropic
            api_client = anthropic.Anthropic(api_key=self._api_key)

            for mkt, stable_mid, current, move, direction in movers:
                sigs = self._haiku_velocity_check(mkt, stable_mid, current, move, direction, api_client)
                if sigs:
                    self._velocity_fired[mkt.id] = now
                    with self._lock:
                        self._pending.extend(sigs)

        except Exception as exc:
            logger.debug(f"[VELOCITY] Scan error: {exc}")

    def _haiku_velocity_check(
        self,
        market,
        stable_price: float,
        current_price: float,
        move: float,
        direction: str,
        api_client,
    ) -> list[NewsArbSignal]:
        """
        Ask Haiku: does this price velocity pattern look like informed trading,
        or random noise? If informed, estimate a new probability.
        """
        pct_move = abs(move) / stable_price * 100 if stable_price > 0 else 0
        prompt = (
            f"A Polymarket prediction market has been trading flat at {stable_price:.3f} YES "
            f"for 30+ minutes, then suddenly moved to {current_price:.3f} "
            f"({'up' if move > 0 else 'down'} {pct_move:.1f}%) in the last "
            f"{VELOCITY_WINDOW_SECS // 60} minutes — before any news article has appeared.\n\n"
            f"MARKET: {market.question}\n"
            f"Current YES price: {current_price:.3f}  |  Move: {move:+.3f}\n\n"
            "Your task: decide if this looks like informed trading (smart money acting on "
            "news not yet published) or random noise/thin-book artifact.\n\n"
            "Informed trading signals:\n"
            "  - The market subject is in the news cycle (geopolitics, policy, elections)\n"
            "  - The direction of the move matches a plausible breaking-news scenario\n"
            "  - The move is sustained (not immediately reversed in the same window)\n\n"
            "Noise signals:\n"
            "  - Very illiquid market (thin book can move on 1 trade)\n"
            "  - Question is obscure / unlikely to have breaking news\n"
            "  - Move is implausibly large (>30pp) with no context\n\n"
            "If this looks like INFORMED TRADING, reply exactly:\n"
            f"INFORMED | NEW_YES_PROB | one-sentence reasoning\n"
            "where NEW_YES_PROB is your estimate of the true current probability (0.00–1.00).\n\n"
            "If this looks like NOISE or a thin-book artifact, reply: NOISE"
        )

        try:
            msg = api_client.messages.create(
                model=HAIKU_MODEL, max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            response = msg.content[0].text.strip()
        except Exception as exc:
            logger.debug(f"[VELOCITY] Haiku error: {exc}")
            return []

        if not response.upper().startswith("INFORMED"):
            logger.debug(f"[VELOCITY] Haiku: NOISE on '{market.question[:50]}'")
            return []

        parts = [p.strip() for p in response.split("|")]
        if len(parts) < 3:
            return []
        try:
            ai_prob   = float(parts[1])
            reasoning = parts[2]
        except (ValueError, IndexError):
            return []

        if not (0.0 < ai_prob < 1.0):
            return []

        edge = abs(ai_prob - current_price)
        if edge < NEWS_ARB_EDGE:
            logger.debug(
                f"[VELOCITY] edge {edge:.1%} < {NEWS_ARB_EDGE:.0%} threshold — skip"
            )
            return []

        token_id = (
            market.yes_token.token_id if direction == "YES"
            else market.no_token.token_id
        )
        hrs = _hours_to_close(market)
        if hrs <= 0:
            return []

        headline = (
            f"Smart money: YES {stable_price:.3f}→{current_price:.3f} "
            f"({move:+.3f}) in {VELOCITY_WINDOW_SECS//60}min — no article yet"
        )
        self.last_headline = headline
        logger.info(
            f"[VELOCITY] SIGNAL {direction} haiku_prob={ai_prob:.2f} mkt={current_price:.3f} "
            f"edge={edge:.1%} '{market.question[:55]}' | {reasoning[:70]}"
        )
        return [NewsArbSignal(
            market_id=market.id,
            question=market.question,
            token_id=token_id,
            side=direction,
            ai_probability=ai_prob,
            market_price=current_price,
            edge=edge,
            headline=headline,
            reasoning=f"[SMART-MONEY] {reasoning}",
            source="PRICE_VELOCITY",
            hours_to_close=hrs,
        )]


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
