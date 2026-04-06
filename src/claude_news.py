"""
Claude Opus AI/Momentum News Analyst — 35% portfolio bucket.

Every 5 minutes this module:
  1. Fetches crypto news from 3 free sources (CryptoPanic, CoinDesk RSS, Decrypt RSS)
  2. Pulls Binance funding rates (sentiment signal — negative = bearish pressure)
  3. Pulls Binance Fear & Greed proxy (BTC 1h momentum direction)
  4. Asks Claude Opus to assess 5-min UP/DOWN probability per asset
  5. Signals when Claude's estimate diverges >AI_EDGE_THRESHOLD (12%) from market
  6. Requires 2+ confirming signals (news + funding OR news + momentum) before trading

Win rate target: 65-75%. Allocation: MOMENTUM_BUDGET_PCT (35%) of wallet.
"""
from __future__ import annotations

import os
import time
import threading
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import requests
from loguru import logger


# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_MODEL        = "claude-opus-4-6"    # Opus for depth; set to claude-sonnet-4-6 for speed
AI_EDGE_THRESHOLD   = 0.12                 # minimum divergence from market to signal
MIN_SIGNALS         = 2                    # require 2+ confirming sub-signals before trading
NEWS_POLL_INTERVAL  = 300                  # align with 5-min window boundary
MAX_NEWS_AGE_SECS   = 600                  # ignore headlines older than 10 minutes

# Free public news sources — no API keys required
_SOURCES = {
    "cryptopanic": "https://cryptopanic.com/api/developer/v2/posts/?currencies=BTC,ETH,SOL,XRP,BNB&filter=important&kind=news",
    "coindesk_rss": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "decrypt_rss":  "https://decrypt.co/feed",
}

# Binance public endpoints for auxiliary signals
_BINANCE_FUNDING   = "https://fapi.binance.com/fapi/v1/premiumIndex"
_BINANCE_KLINE_1H  = "https://api.binance.com/api/v3/klines?symbol={sym}USDT&interval=1h&limit=2"


@dataclass
class AISignal:
    asset:          str
    direction:      str          # "YES" (up) | "NO" (down)
    ai_probability: float
    market_price:   float
    edge:           float
    headline:       str
    reasoning:      str
    confirming:     int = 0      # number of confirming sub-signals (news, funding, momentum)
    ts:             float = field(default_factory=time.time)


class ClaudeNewsAnalyst:
    """
    Singleton that polls multi-source news + market signals every 5 minutes,
    uses Claude Opus to assess probabilities, and emits trade signals.
    """

    def __init__(self):
        self._api_key  = os.getenv("ANTHROPIC_API_KEY", "")
        self._lock     = threading.Lock()
        self.pending:  list[AISignal] = []
        self._last_run = 0.0
        self._thread:  Optional[threading.Thread] = None
        # CryptoPanic webhook pushes headlines here in real-time
        self._incoming: deque[str] = deque(maxlen=50)

    def push_headline(self, title: str) -> None:
        """Called by the /webhook/cryptopanic endpoint to inject a breaking headline."""
        if title:
            self._incoming.append(title.strip())
            logger.info(f"[CLAUDE-NEWS] Webhook headline received: {title[:80]}")

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    # ── Public API ─────────────────────────────────────────────────────────────

    def maybe_run(self, market_prices: dict[str, float]) -> None:
        if not self.available:
            return
        now = time.time()
        if now - self._last_run < NEWS_POLL_INTERVAL:
            return
        if self._thread and self._thread.is_alive():
            return
        self._last_run = now
        self._thread = threading.Thread(
            target=self._run, args=(dict(market_prices),),
            daemon=True, name="claude-news",
        )
        self._thread.start()

    def pop_signals(self) -> list[AISignal]:
        with self._lock:
            sigs = list(self.pending)
            self.pending.clear()
        return sigs

    # ── Data fetching ──────────────────────────────────────────────────────────

    def _fetch_news(self) -> list[str]:
        """Fetch headlines from all configured sources, return deduplicated list."""
        headlines: list[str] = []
        cutoff = time.time() - MAX_NEWS_AGE_SECS

        # Source 1: CryptoPanic JSON API (requires free auth_token — set CRYPTOPANIC_TOKEN in .env)
        import os as _os
        _cp_token = _os.getenv("CRYPTOPANIC_TOKEN", "")
        if _cp_token:
            try:
                url = _SOURCES["cryptopanic"] + "&auth_token=" + _cp_token
                r = requests.get(url, timeout=8)
                r.raise_for_status()
            except Exception as exc:
                logger.debug(f"[CLAUDE-NEWS] CryptoPanic failed: {exc}")
                r = None
        else:
            r = None
        if r is not None:
            try:
                for post in (r.json().get("results") or [])[:15]:
                    try:
                        import datetime
                        ts = datetime.datetime.fromisoformat(
                            post.get("created_at", "").replace("Z", "+00:00")
                        ).timestamp()
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                    title = (post.get("title") or "").strip()
                    if title:
                        headlines.append(title)
            except Exception as exc:
                logger.debug(f"[CLAUDE-NEWS] CryptoPanic parse error: {exc}")

        # Sources 2 & 3: RSS feeds (CoinDesk, Decrypt)
        for src_name, url in [("coindesk_rss", _SOURCES["coindesk_rss"]),
                               ("decrypt_rss",  _SOURCES["decrypt_rss"])]:
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                root = ET.fromstring(r.text)
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").strip()
                    if title and title not in headlines:
                        headlines.append(title)
                    if len(headlines) >= 20:
                        break
            except Exception as exc:
                logger.debug(f"[CLAUDE-NEWS] {src_name} RSS failed: {exc}")

        # Prepend any real-time webhook headlines (highest priority — most breaking)
        webhook_headlines = list(self._incoming)
        for h in reversed(webhook_headlines):
            if h not in headlines:
                headlines.insert(0, h)
        self._incoming.clear()

        return headlines[:20]

    def _fetch_funding_rates(self) -> dict[str, float]:
        """
        Binance perpetual funding rates. Negative = market is net short (bearish).
        Returns dict of symbol → rate, e.g. {"BTC": -0.0001, "ETH": 0.0002}.
        """
        rates: dict[str, float] = {}
        _SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
                    "XRPUSDT": "XRP", "BNBUSDT": "BNB"}
        try:
            r = requests.get(_BINANCE_FUNDING, timeout=6)
            r.raise_for_status()
            for item in r.json():
                sym = item.get("symbol", "")
                asset = _SYMBOLS.get(sym)
                if asset:
                    rates[asset] = float(item.get("lastFundingRate", 0))
        except Exception as exc:
            logger.debug(f"[CLAUDE-NEWS] Funding rates failed: {exc}")
        return rates

    def _fetch_1h_momentum(self, asset: str) -> Optional[float]:
        """1-hour return for an asset from Binance. Positive = bullish."""
        sym = asset if asset != "BNB" else "BNB"
        try:
            r = requests.get(
                _BINANCE_KLINE_1H.format(sym=sym), timeout=5
            )
            r.raise_for_status()
            candles = r.json()
            if len(candles) >= 2:
                prev_close = float(candles[0][4])
                curr_close = float(candles[1][4])
                return (curr_close - prev_close) / prev_close
        except Exception:
            pass
        return None

    # ── Analysis ───────────────────────────────────────────────────────────────

    def _run(self, market_prices: dict[str, float]) -> None:
        try:
            headlines    = self._fetch_news()
            funding      = self._fetch_funding_rates()

            assets = list(market_prices.keys())
            momentum_1h  = {a: self._fetch_1h_momentum(a) for a in assets}

            if not headlines:
                logger.debug("[CLAUDE-NEWS] No fresh headlines — skipping")
                return

            signals = self._analyze(headlines, market_prices, funding, momentum_1h)
            with self._lock:
                self.pending.extend(signals)
            if signals:
                logger.info(
                    f"[CLAUDE-NEWS] {len(signals)} AI signal(s): "
                    + ", ".join(
                        f"{s.asset} {s.direction} edge={s.edge:.1%} ({s.confirming} confirmations)"
                        for s in signals
                    )
                )
        except Exception as exc:
            logger.debug(f"[CLAUDE-NEWS] Run error: {exc}")

    def _analyze(
        self,
        headlines:   list[str],
        market_prices: dict[str, float],
        funding:     dict[str, float],
        momentum_1h: dict[str, Optional[float]],
    ) -> list[AISignal]:
        import anthropic

        assets_block = []
        for asset, mid in sorted(market_prices.items()):
            if mid <= 0:
                continue
            fr   = funding.get(asset)
            mom  = momentum_1h.get(asset)
            fr_str  = f"funding={fr:+.4%}" if fr is not None else "funding=n/a"
            mom_str = f"1h_return={mom:+.3%}" if mom is not None else "1h=n/a"
            assets_block.append(f"  {asset}: YES@{mid:.3f} ({fr_str}, {mom_str})")

        news_block = "\n".join(f"  - {h}" for h in headlines[:15])

        prompt = f"""You are a crypto momentum analyst. Given the news and market data below, assess the probability each crypto will be HIGHER in price in the next 5 minutes on Polymarket.

MARKET DATA (YES = higher in 5 min):
{chr(10).join(assets_block)}

RECENT HEADLINES (<10 min old):
{news_block}

INSTRUCTIONS:
- Consider: news sentiment, funding rate direction, 1h momentum, current market pricing
- Negative funding = short pressure = bearish bias
- Positive 1h return = bullish momentum
- Only signal when you have HIGH conviction AND market price is significantly wrong
- Format exactly: ASSET: 0.XX | one-sentence reason
- If no strong edge on an asset, skip it entirely
- If nothing is tradeable, reply: NO_SIGNAL"""

        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            response = msg.content[0].text.strip()
        except Exception as exc:
            logger.debug(f"[CLAUDE-NEWS] Anthropic API error: {exc}")
            return []

        if "NO_SIGNAL" in response:
            logger.debug("[CLAUDE-NEWS] Claude: no signals this cycle")
            return []

        signals: list[AISignal] = []
        _valid_lines = [l for l in response.splitlines()
                        if l.strip() and ":" in l and "|" in l]
        if _valid_lines:
            self._send_news_webhook(headlines, _valid_lines, market_prices)
        for line in response.splitlines():
            line = line.strip()
            if not line or ":" not in line or "|" not in line:
                continue
            try:
                asset_part, rest = line.split(":", 1)
                prob_str, reasoning = rest.split("|", 1)
                asset     = asset_part.strip().upper()
                ai_prob   = float(prob_str.strip())
                reasoning = reasoning.strip()
            except (ValueError, IndexError):
                continue

            mid = market_prices.get(asset, 0.0)
            if mid <= 0:
                continue

            edge = abs(ai_prob - mid)
            if edge < AI_EDGE_THRESHOLD:
                continue

            direction = "YES" if ai_prob > mid else "NO"

            # Count confirming sub-signals
            confirming = 1  # Claude itself = 1
            fr  = funding.get(asset)
            mom = momentum_1h.get(asset)

            if direction == "YES":
                if fr is not None and fr < -0.0001:
                    confirming += 1   # negative funding = short pressure = up squeeze potential
                if mom is not None and mom > 0.001:
                    confirming += 1   # positive 1h momentum
            else:  # "NO" = DOWN
                if fr is not None and fr > 0.0001:
                    confirming += 1   # positive funding = over-levered longs = down pressure
                if mom is not None and mom < -0.001:
                    confirming += 1   # negative 1h momentum

            if confirming < MIN_SIGNALS:
                logger.debug(
                    f"[CLAUDE-NEWS] {asset} {direction} skipped — "
                    f"only {confirming}/{MIN_SIGNALS} confirming signals"
                )
                continue

            signals.append(AISignal(
                asset=asset, direction=direction,
                ai_probability=ai_prob, market_price=mid,
                edge=edge, headline=headlines[0] if headlines else "",
                reasoning=reasoning, confirming=confirming,
            ))

        return signals

    def _send_news_webhook(
        self,
        headlines: list[str],
        signal_lines: list[str],
        market_prices: dict[str, float],
    ) -> None:
        """Fire a Discord/Telegram alert before AI signals are executed."""
        import os as _os, requests as _rq

        msg_lines = ["**[CLAUDE-NEWS] AI signals detected**"]
        msg_lines.append(f"Top headline: _{headlines[0][:120] if headlines else 'n/a'}_")
        msg_lines.append("")
        for line in signal_lines[:5]:
            msg_lines.append(f"• `{line.strip()[:100]}`")

        text = "\n".join(msg_lines)

        discord_url = _os.getenv("DISCORD_WEBHOOK_URL", "")
        if discord_url:
            try:
                _rq.post(discord_url, json={"content": text}, timeout=5)
            except Exception as exc:
                logger.debug(f"[CLAUDE-NEWS] Discord webhook failed: {exc}")

        tg_token = _os.getenv("TELEGRAM_BOT_TOKEN", "")
        tg_chat  = _os.getenv("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            try:
                _rq.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": text, "parse_mode": "Markdown"},
                    timeout=5,
                )
            except Exception as exc:
                logger.debug(f"[CLAUDE-NEWS] Telegram webhook failed: {exc}")


# Module-level singleton
analyst = ClaudeNewsAnalyst()
