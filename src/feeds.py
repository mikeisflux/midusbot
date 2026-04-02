"""
Live data feeds for MIDUSBOT.

BinanceWSFeed
─────────────
Connects to Binance's combined WebSocket stream and keeps a real-time
price cache updated for BTC, ETH, SOL, XRP, DOGE, BNB, and HYPE.
Prices are written directly into strategy._PRICE_CACHE so every
strategy reads sub-100 ms data instead of 2-second stale REST polls.

This is what separates the 0x8dxd 98% win-rate bot from a polling bot:
by the time a REST call completes, the 2.7-second Polymarket lag window
has already closed.  WebSocket keeps us ahead of it.

NewsFeed
────────
Polls public RSS feeds (CoinDesk, Cointelegraph, Reuters Finance, BBG)
every 60 seconds for new headlines.  Headlines are cross-referenced
against active Polymarket market questions.  When a relevant headline
appears, that market gets priority attention from NewsEventStrategy on
the next loop — potentially before the Polymarket price has moved at all.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable

import requests
from loguru import logger

# ---------------------------------------------------------------------------
# Binance WebSocket price feed
# ---------------------------------------------------------------------------

# Map internal symbol → Binance stream name
_WS_SYMBOLS: dict[str, str] = {
    "BTC":  "btcusdt",
    "ETH":  "ethusdt",
    "SOL":  "solusdt",
    "XRP":  "xrpusdt",
    "DOGE": "dogeusdt",
    "BNB":  "bnbusdt",
}

# HYPE is not on Binance — fetch via REST fallback handled by strategy.py


class BinanceWSFeed:
    """
    Maintains a live WebSocket connection to Binance and continuously
    updates strategy._PRICE_CACHE with real-time prices.

    Usage:
        feed = BinanceWSFeed()
        feed.start()          # non-blocking — spawns background thread
        ...
        feed.stop()
    """

    # Port 9443 is often blocked by VPS firewalls; port 443 is always open
    _WS_URL = (
        "wss://stream.binance.com:443/stream?streams="
        + "/".join(f"{sym}@aggTrade" for sym in _WS_SYMBOLS.values())
    )

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._ws = None
        self._running = False
        self._last_prices: dict[str, float] = {}
        self._on_price: list[Callable[[str, float], None]] = []

    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="binance-ws")
        self._thread.start()
        logger.info("BinanceWSFeed started — real-time prices active")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def latest(self, symbol: str) -> float | None:
        return self._last_prices.get(symbol.upper())

    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Background thread: connect and auto-reconnect on failure."""
        backoff = 1
        while self._running:
            try:
                import websocket
                self._ws = websocket.WebSocketApp(
                    self._WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                logger.warning(f"BinanceWSFeed error: {exc}")
            if not self._running:
                break
            logger.info(f"BinanceWSFeed reconnecting in {backoff}s…")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _on_open(self, ws) -> None:
        logger.info("BinanceWSFeed connected ✓")
        backoff = 1  # reset on successful connect

    def _on_close(self, ws, code, msg) -> None:
        logger.warning(f"BinanceWSFeed disconnected (code={code})")

    def _on_error(self, ws, error) -> None:
        logger.warning(f"BinanceWSFeed error: {error}")

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
            data = msg.get("data", {})
            stream = msg.get("stream", "")   # e.g. "btcusdt@aggTrade"
            price_str = data.get("p")        # aggTrade price field
            if not price_str:
                return

            price = float(price_str)

            # Reverse-lookup: stream prefix → our symbol
            sym_lower = stream.split("@")[0].replace("usdt", "")
            symbol = sym_lower.upper()
            if symbol == "SOLUSD" or sym_lower == "sol":
                symbol = "SOL"

            # Update strategy price cache directly so strategies read real-time data
            _update_strategy_cache(symbol, price)
            self._last_prices[symbol] = price

        except Exception:
            pass


def _update_strategy_cache(symbol: str, price: float) -> None:
    """Write a fresh price into strategy._PRICE_CACHE and _PRICE_HISTORY."""
    try:
        import src.strategy as _strat
        now = time.time()
        _strat._PRICE_CACHE[symbol] = (price, now)
        hist = _strat._PRICE_HISTORY.setdefault(symbol, [])
        hist.append((price, now))
        cutoff = now - _strat._HISTORY_WINDOW
        _strat._PRICE_HISTORY[symbol] = [(p, t) for p, t in hist if t >= cutoff]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# News RSS feed
# ---------------------------------------------------------------------------

_RSS_SOURCES = [
    # Crypto-native
    ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph",  "https://cointelegraph.com/rss"),
    ("Decrypt",        "https://decrypt.co/feed"),
    ("TheBlock",       "https://www.theblock.co/rss.xml"),
    # Macro / finance
    ("Reuters-Biz",    "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters-Tech",   "https://feeds.reuters.com/reuters/technologyNews"),
    ("AP-Business",    "https://feeds.apnews.com/apnews/Business"),
    ("AP-Politics",    "https://feeds.apnews.com/apnews/Politics"),
    # Prediction-market relevant
    ("Axios",          "https://api.axios.com/feed/"),
    ("BBC-World",      "https://feeds.bbci.co.uk/news/world/rss.xml"),
]

# Keywords relevant to prediction markets — crypto, macro, political, geopolitical
# (sports removed — bot no longer trades sports markets)
_MARKET_KEYWORDS = (
    # Crypto assets
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple",
    "dogecoin", "doge", "bnb", "binance", "crypto", "defi", "nft", "stablecoin",
    "polymarket", "coinbase", "sec", "etf", "spot etf",
    # Macro / Fed
    "fed", "federal reserve", "rate", "interest rate", "inflation", "cpi", "pce",
    "fomc", "powell", "gdp", "recession", "jobs", "nfp", "payroll", "treasury",
    # Political / geopolitical
    "trump", "president", "election", "congress", "senate", "tariff", "sanction",
    "ukraine", "russia", "china", "taiwan", "war", "ceasefire", "nato",
    "iran", "north korea", "israel", "gaza",
    # Markets / finance
    "nasdaq", "s&p", "dow", "oil", "gold", "bank", "ipo", "default", "debt",
    "dollar", "yen", "euro", "currency",
)


class Headline:
    __slots__ = ("title", "url", "source", "published_ts", "score")

    def __init__(self, title: str, url: str, source: str, published_ts: float, score: float = 0.0):
        self.title        = title
        self.url          = url
        self.source       = source
        self.published_ts = published_ts
        self.score        = score   # relevance score vs active markets

    def __repr__(self) -> str:
        return f"[{self.source}] {self.title[:80]}"


class NewsFeed:
    """
    Polls public RSS feeds every POLL_INTERVAL seconds.
    Tracks seen URLs to detect only new headlines.
    Scores each headline for relevance to prediction markets.

    Usage:
        feed = NewsFeed()
        feed.start()               # background polling thread
        new = feed.drain_new()     # returns new headlines since last call
    """

    POLL_INTERVAL = 60   # seconds between RSS polls
    MAX_AGE_HOURS  = 6    # ignore headlines older than this
    MEMORY_HOURS   = 24   # keep recent headlines in memory for context lookup
    MEMORY_MAX     = 500  # cap on stored headlines

    def __init__(self) -> None:
        self._seen_urls: set[str] = set()
        self._new: list[Headline] = []
        self._memory: list[Headline] = []   # rolling 24h headline buffer
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="news-feed")
        self._thread.start()
        logger.info("NewsFeed started — polling RSS sources")

    def stop(self) -> None:
        self._running = False

    def drain_new(self) -> list[Headline]:
        """Return and clear the buffer of new headlines."""
        with self._lock:
            items = list(self._new)
            self._new.clear()
        return items

    def score_against_markets(self, headline: Headline, market_questions: list[str]) -> float:
        """
        Rate how likely `headline` is to move prices in any of the given markets.
        Returns 0.0–1.0.
        """
        title_lower = headline.title.lower()
        best = 0.0
        for q in market_questions:
            q_lower = q.lower()
            # Extract significant words from the question (skip short stop words)
            q_words = {w for w in re.findall(r"[a-z]{4,}", q_lower)}
            title_words = set(re.findall(r"[a-z]{4,}", title_lower))
            overlap = q_words & title_words
            if not overlap:
                continue
            score = len(overlap) / max(len(q_words), 1)
            best = max(best, score)
        return best

    def recent_for_market(self, question: str, max_results: int = 5, min_score: float = 0.15) -> list[Headline]:
        """
        Return up to max_results recent headlines from the 24h memory that are
        relevant to the given market question. Used by strategies to get news
        context before deciding whether to trade.
        """
        q_words = {w for w in re.findall(r"[a-z]{4,}", question.lower())}
        if not q_words:
            return []
        scored: list[tuple[float, Headline]] = []
        with self._lock:
            for h in self._memory:
                title_words = set(re.findall(r"[a-z]{4,}", h.title.lower()))
                overlap = q_words & title_words
                if not overlap:
                    continue
                score = len(overlap) / max(len(q_words), 1)
                if score >= min_score:
                    scored.append((score, h))
        scored.sort(key=lambda x: (-x[0], -x[1].published_ts))
        return [h for _, h in scored[:max_results]]

    # ------------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            self._poll()
            for _ in range(self.POLL_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)

    def _poll(self) -> None:
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser not installed — news feed disabled (pip install feedparser)")
            self._running = False
            return

        now = time.time()
        max_age_secs = self.MAX_AGE_HOURS * 3600
        new_this_poll: list[Headline] = []

        for source_name, url in _RSS_SOURCES:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    link = entry.get("link", "")
                    if not link or link in self._seen_urls:
                        continue

                    # Parse publish time
                    pub_ts = now
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        import calendar
                        pub_ts = float(calendar.timegm(entry.published_parsed))

                    if now - pub_ts > max_age_secs:
                        continue   # too old

                    self._seen_urls.add(link)

                    title = entry.get("title", "")
                    title_lower = title.lower()

                    # Quick relevance pre-filter
                    if not any(kw in title_lower for kw in _MARKET_KEYWORDS):
                        continue

                    h = Headline(
                        title=title,
                        url=link,
                        source=source_name,
                        published_ts=pub_ts,
                    )
                    new_this_poll.append(h)

            except Exception as exc:
                logger.debug(f"NewsFeed error ({source_name}): {exc}")

        if new_this_poll:
            logger.info(f"[NEWS] {len(new_this_poll)} new headline(s): "
                        + " | ".join(h.title[:40] for h in new_this_poll[:3]))
            with self._lock:
                self._new.extend(new_this_poll)
                # Add to rolling memory buffer, evict headlines older than MEMORY_HOURS
                cutoff = now - self.MEMORY_HOURS * 3600
                self._memory = [h for h in self._memory if h.published_ts > cutoff]
                self._memory.extend(new_this_poll)
                if len(self._memory) > self.MEMORY_MAX:
                    self._memory = self._memory[-self.MEMORY_MAX:]
