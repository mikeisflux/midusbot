"""
Shared utilities for MIDUSBOT.

  atomic_json_write  — crash-safe JSON file writes via tmp+rename
  CircuitBreaker     — stops hammering APIs that are down
  Alerter            — Telegram / Discord webhook notifications
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

import config

# ---------------------------------------------------------------------------
# 1. Atomic JSON writes
# ---------------------------------------------------------------------------

def atomic_json_write(path: Path | str, data: Any, indent: int = 2) -> None:
    """
    Write JSON atomically: serialise → tmp file in same dir → os.replace().
    os.replace() is atomic on Linux (rename syscall), so a crash mid-write
    never leaves a corrupt file — the old file survives intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 2. Circuit breaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    Prevents hammering a failing API endpoint.

    States:
      CLOSED   — normal, requests pass through
      OPEN     — too many failures, requests blocked for `reset_secs`
      HALF     — one probe request allowed after cooldown

    Usage:
        _cb = CircuitBreaker("Gamma API", failure_threshold=5, reset_secs=60)

        if not _cb.allow():
            return None          # skip — circuit is open
        try:
            result = requests.get(...)
            _cb.success()
            return result
        except Exception as exc:
            _cb.failure()
            raise
    """

    def __init__(self, name: str, failure_threshold: int = 5, reset_secs: float = 60.0):
        self._name      = name
        self._threshold = failure_threshold
        self._reset     = reset_secs
        self._failures  = 0
        self._opened_at: float | None = None
        self._lock      = threading.Lock()

    # -- state -----------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_unlocked()

    def _state_unlocked(self) -> str:
        if self._opened_at is None:
            return "CLOSED"
        if time.time() - self._opened_at >= self._reset:
            return "HALF"
        return "OPEN"

    # -- public API ------------------------------------------------------------

    def allow(self) -> bool:
        with self._lock:
            s = self._state_unlocked()
            if s == "CLOSED":
                return True
            if s == "HALF":
                return True   # probe attempt
            # OPEN
            remaining = self._reset - (time.time() - self._opened_at)
            logger.debug(
                f"[CB] {self._name} OPEN — blocking request "
                f"({remaining:.0f}s until retry)"
            )
            return False

    def success(self) -> None:
        with self._lock:
            if self._failures > 0:
                logger.info(f"[CB] {self._name} recovered — circuit CLOSED")
            self._failures  = 0
            self._opened_at = None

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold and self._opened_at is None:
                self._opened_at = time.time()
                logger.warning(
                    f"[CB] {self._name} OPEN after {self._failures} failures "
                    f"— pausing for {self._reset:.0f}s"
                )


# ---------------------------------------------------------------------------
# 3. Alerter — Telegram + Discord
# ---------------------------------------------------------------------------

class Alerter:
    """
    Sends notifications to Telegram and/or Discord on important events.

    Configure in .env:
        TELEGRAM_BOT_TOKEN=<token>
        TELEGRAM_CHAT_ID=<chat_id>
        DISCORD_WEBHOOK_URL=<url>

    All sends are fire-and-forget in a daemon thread so they never block
    the trading loop.
    """

    TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self) -> None:
        self._tg_token  = getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""
        self._tg_chat   = getattr(config, "TELEGRAM_CHAT_ID",   "") or ""
        self._discord   = getattr(config, "DISCORD_WEBHOOK_URL", "") or ""
        self._enabled   = bool(
            (self._tg_token and self._tg_chat) or self._discord
        )
        if self._enabled:
            channels = []
            if self._tg_token and self._tg_chat:
                channels.append("Telegram")
            if self._discord:
                channels.append("Discord")
            logger.info(f"[Alerter] Active on: {', '.join(channels)}")
            # Send a startup ping so we know alerts are working
            self.send("Bot started ✓", level="info", blocking=True)

    def send(self, text: str, level: str = "info", blocking: bool = False) -> None:
        """
        Send an alert. blocking=True waits for delivery (use for shutdown/startup).
        Non-blocking by default so alerts never delay the trading loop.
        """
        if not self._enabled:
            return
        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        msg = f"{prefix} *MIDUSBOT* — {text}"
        if blocking:
            self._send_sync(msg)
        else:
            threading.Thread(
                target=self._send_sync, args=(msg,), daemon=True, name="alerter"
            ).start()

    def _send_sync(self, msg: str) -> None:
        import requests as _req
        if self._tg_token and self._tg_chat:
            try:
                _req.post(
                    self.TELEGRAM_URL.format(token=self._tg_token),
                    json={"chat_id": self._tg_chat, "text": msg, "parse_mode": "Markdown"},
                    timeout=10,
                )
            except Exception as exc:
                logger.debug(f"[Alerter] Telegram failed: {exc}")

        if self._discord:
            try:
                _req.post(
                    self._discord,
                    json={"content": msg},
                    timeout=10,
                )
            except Exception as exc:
                logger.debug(f"[Alerter] Discord failed: {exc}")


# Singleton — imported and used directly
alerter = Alerter()
