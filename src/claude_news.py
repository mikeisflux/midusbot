"""
Claude Opus AI/Momentum News Analyst — 35% portfolio bucket.

Every 5 minutes (aligned to window boundaries) this module:
  1. Fetches crypto news headlines from CryptoPanic (free public API, no auth)
  2. Fetches current Polymarket UpDown market prices from the bot's market cache
  3. Asks Claude to assign a probability for each market given the news context
  4. When Claude's estimate diverges >AI_EDGE_THRESHOLD from market price, emits
     a TradingSignal for the scanner to execute

This is the "AI-Powered Probability Arbitrage" leg of the aggressive portfolio.
Win rate target: 65-75%. Allocation: MOMENTUM_BUDGET_PCT (35%) of wallet.
"""
from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

import requests
from loguru import logger


# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_MODEL        = "claude-sonnet-4-6"   # sonnet for speed; opus for depth
AI_EDGE_THRESHOLD   = 0.12   # only signal when AI estimate > 12% from market
NEWS_POLL_INTERVAL  = 300    # seconds — realign with 5-min window boundary
MAX_NEWS_AGE_SECS   = 600    # ignore headlines older than 10 minutes
CRYPTOPANIC_URL     = "https://cryptopanic.com/api/v1/posts/?public=true&currencies=BTC,ETH,SOL,XRP,BNB&filter=important&kind=news"


@dataclass
class AISignal:
    """A trade signal emitted by the Claude news analyst."""
    asset:          str          # BTC, ETH, SOL etc.
    direction:      str          # "YES" (up) | "NO" (down)
    ai_probability: float        # Claude's estimate (0-1)
    market_price:   float        # current YES mid
    edge:           float        # abs(ai_prob - market_price)
    headline:       str          # triggering headline
    reasoning:      str          # Claude's explanation
    ts:             float = field(default_factory=time.time)


class ClaudeNewsAnalyst:
    """
    Singleton that polls news and emits AI-driven trade signals.
    Signals are queued in self.pending and consumed by the scanner.
    """

    def __init__(self):
        self._api_key   = os.getenv("ANTHROPIC_API_KEY", "")
        self._lock      = threading.Lock()
        self.pending:   list[AISignal] = []   # consumed by scanner each loop
        self._last_run  = 0.0
        self._thread: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    # ── Public API ─────────────────────────────────────────────────────────────

    def maybe_run(self, market_prices: dict[str, float]) -> None:
        """
        Call from the main loop. Launches analysis in a background thread
        if NEWS_POLL_INTERVAL has elapsed and Claude is available.
        `market_prices`: dict of asset→YES_mid_price from current CLOB data.
        """
        if not self.available:
            return
        now = time.time()
        if now - self._last_run < NEWS_POLL_INTERVAL:
            return
        if self._thread and self._thread.is_alive():
            return  # previous run still going

        self._last_run = now
        self._thread = threading.Thread(
            target=self._run,
            args=(dict(market_prices),),
            daemon=True,
            name="claude-news",
        )
        self._thread.start()

    def pop_signals(self) -> list[AISignal]:
        """Return and clear all pending signals."""
        with self._lock:
            sigs = list(self.pending)
            self.pending.clear()
        return sigs

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self, market_prices: dict[str, float]) -> None:
        try:
            headlines = self._fetch_news()
            if not headlines:
                logger.debug("[CLAUDE-NEWS] No fresh headlines — skipping analysis")
                return
            signals = self._analyze(headlines, market_prices)
            with self._lock:
                self.pending.extend(signals)
            if signals:
                logger.info(
                    f"[CLAUDE-NEWS] {len(signals)} AI signal(s): "
                    + ", ".join(f"{s.asset} {s.direction} edge={s.edge:.1%}" for s in signals)
                )
        except Exception as exc:
            logger.debug(f"[CLAUDE-NEWS] Run failed: {exc}")

    def _fetch_news(self) -> list[str]:
        """Fetch important crypto headlines from CryptoPanic (no auth required)."""
        try:
            resp = requests.get(CRYPTOPANIC_URL, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            cutoff = time.time() - MAX_NEWS_AGE_SECS
            headlines = []
            for post in (data.get("results") or [])[:15]:
                created = post.get("created_at", "")
                # Quick ISO parse — CryptoPanic returns "2026-04-06T01:30:00Z"
                try:
                    import datetime
                    ts = datetime.datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    ).timestamp()
                    if ts < cutoff:
                        continue
                except Exception:
                    pass
                title = post.get("title", "").strip()
                if title:
                    headlines.append(title)
            return headlines
        except Exception as exc:
            logger.debug(f"[CLAUDE-NEWS] News fetch failed: {exc}")
            return []

    def _analyze(
        self,
        headlines: list[str],
        market_prices: dict[str, float],
    ) -> list[AISignal]:
        """Send headlines + prices to Claude, parse probability estimates."""
        import anthropic

        assets_info = "\n".join(
            f"  {asset}: YES currently trading at {price:.3f} (market implies {price:.1%} prob UP)"
            for asset, price in sorted(market_prices.items())
            if price > 0
        )
        news_text = "\n".join(f"  - {h}" for h in headlines)

        prompt = f"""You are a crypto prediction market analyst. Evaluate the following breaking news headlines and assess the probability that each cryptocurrency will be HIGHER in price in the next 5 minutes.

CURRENT POLYMARKET PRICES (YES = will be higher in 5 min):
{assets_info}

RECENT HEADLINES (last 10 minutes):
{news_text}

For each asset (BTC, ETH, SOL, XRP, BNB), provide:
1. Your probability estimate (0.00-1.00) that price will be UP in 5 minutes
2. One-sentence reasoning

Respond in this EXACT format (one line per asset):
BTC: 0.XX | reason
ETH: 0.XX | reason
SOL: 0.XX | reason
XRP: 0.XX | reason
BNB: 0.XX | reason

Only include assets where you have high conviction (>15% edge vs current price).
If no strong signal, reply: NO_SIGNAL"""

        try:
            client  = anthropic.Anthropic(api_key=self._api_key)
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            response = message.content[0].text.strip()
        except Exception as exc:
            logger.debug(f"[CLAUDE-NEWS] Anthropic API error: {exc}")
            return []

        if "NO_SIGNAL" in response:
            logger.debug("[CLAUDE-NEWS] Claude: no strong signals this cycle")
            return []

        signals = []
        for line in response.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            try:
                parts = line.split(":", 1)
                asset = parts[0].strip().upper()
                rest  = parts[1].strip()
                prob_str, reasoning = rest.split("|", 1)
                ai_prob = float(prob_str.strip())
                reasoning = reasoning.strip()
            except (ValueError, IndexError):
                continue

            market_mid = market_prices.get(asset, 0.0)
            if market_mid <= 0:
                continue

            edge = abs(ai_prob - market_mid)
            if edge < AI_EDGE_THRESHOLD:
                continue

            direction = "YES" if ai_prob > market_mid else "NO"
            signals.append(AISignal(
                asset=asset,
                direction=direction,
                ai_probability=ai_prob,
                market_price=market_mid,
                edge=edge,
                headline=headlines[0] if headlines else "",
                reasoning=reasoning,
            ))

        return signals


# Module-level singleton
analyst = ClaudeNewsAnalyst()
