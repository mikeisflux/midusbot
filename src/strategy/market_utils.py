"""
Market detection helpers — parse Polymarket question strings.

Functions:
  _detect_updown_market   — returns asset symbol if question is a 5/15-min UpDown market
  _updown_window_mins     — extract window duration in minutes from question text
  _market_seconds_into_window — elapsed seconds since market window opened
"""
from __future__ import annotations

import re as _re
import time
from datetime import datetime

from src.strategy.constants import _UPDOWN_ASSETS


def _detect_updown_market(question: str) -> str | None:
    """
    Returns the asset symbol if the question is an Up/Down short-interval
    crypto market.  Handles all Polymarket question phrasings:
      • "XRP Up or Down - March 3, 12:00PM-12:05PM ET"
      • "Will BTC be higher or lower in 15 minutes?"
      • "Bitcoin higher or lower in the next 5 min?"
      • "BTC up/down 15m"
    Returns None otherwise.
    """
    q = question.lower()
    direction_phrases = (
        "up or down", "up/down", "higher or lower",
        "higher in", "lower in", "go up", "go down",
        "be up", "be down",
    )
    # Must reference a short time interval to avoid false positives on
    # long-term questions like "Will BTC be higher by end of year?"
    time_phrases = (
        "5 min", "5min", "15 min", "15min", "5-min", "15-min",
        "5 minute", "15 minute", "next 5", "next 15",
        "in 5", "in 15", ":00pm", ":05pm", ":10pm", ":15pm",
        ":20pm", ":25pm", ":30pm", ":35pm", ":40pm", ":45pm",
        ":50pm", ":55pm", ":00am", ":05am", ":10am", ":15am",
    )
    has_direction = any(p in q for p in direction_phrases)
    has_time      = any(p in q for p in time_phrases)
    if not has_direction:
        return None
    if "higher or lower" in q and not has_time:
        return None
    for kw, sym in _UPDOWN_ASSETS.items():
        if kw in q:
            import config as _cfg
            if _cfg.ALLOWED_ASSETS and sym not in _cfg.ALLOWED_ASSETS:
                return None
            return sym
    return None


def _updown_window_mins(question: str) -> int | None:
    """
    Extract the time-window duration in minutes from an UpDown market question.

    Examples:
      "BTC Up or Down - 3:00AM-3:05AM ET"  → 5
      "BTC Up or Down - 3AM-4AM ET"         → 60
      "BTC 5 Minute Up or Down"             → 5
      "BTC 15 Minute Up or Down"            → 15
      "BTC 1 Hour Up or Down"               → 60
      "BTC 4 Hour Up or Down"               → 240

    Returns None if the format isn't recognised.
    """
    q = question.lower()
    # New perpetual format: "N Minute" or "N Hour"
    m = _re.search(r'(\d+)\s*(minute|min)\b', q)
    if m:
        return int(m.group(1))
    m = _re.search(r'(\d+)\s*(hour|hr)\b', q)
    if m:
        return int(m.group(1)) * 60
    # Old per-slot format: "H:MMam-H:MMam"
    m = _re.search(r'(\d{1,2}):(\d{2})\s*[ap]m\s*[-–]\s*(\d{1,2}):(\d{2})\s*[ap]m', q)
    if m:
        h1, mn1, h2, mn2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        delta = (h2 * 60 + mn2) - (h1 * 60 + mn1)
        if delta <= 0:
            delta += 12 * 60  # AM/PM rollover
        return delta
    # Old hourly format: "HAM-H+1AM"
    m = _re.search(r'(\d{1,2})\s*[ap]m\s*[-–]\s*(\d{1,2})\s*[ap]m', q)
    if m:
        h1, h2 = int(m.group(1)), int(m.group(2))
        delta = (h2 - h1) * 60
        if delta <= 0:
            delta += 12 * 60
        return delta
    return None


def _market_seconds_into_window(market) -> float | None:
    """
    Returns how many seconds have elapsed since this market window opened.
    None if we can't determine timing.
    """
    end_date = getattr(market, "end_date", None) or getattr(market, "end_utc", None)
    if not end_date:
        return None
    try:
        from datetime import timezone as _tz
        end_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
        window_mins = _updown_window_mins(market.question) or 5
        start_ts = end_ts - window_mins * 60
        return time.time() - start_ts
    except Exception:
        return None
