"""
Discord two-way command interface for MIDUSBOT.

Polls the configured channel for messages starting with ! and executes
commands. Responds via the same channel using the bot token.

Commands:
  !status    — mode, balance, open positions, win rate, P&L
  !stop      — graceful shutdown
  !pause     — pause new trades (keeps managing positions)
  !resume    — resume trading
  !live      — switch to live trading
  !dry       — switch to dry-run
  !refactor <text> — trigger LLM analyst with a custom instruction

Runs as a daemon thread — never blocks the trading loop.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

import requests
from loguru import logger

import config

if TYPE_CHECKING:
    from src.dashboard import DashboardState

DISCORD_API = "https://discord.com/api/v10"
POLL_INTERVAL = 3   # seconds between message checks


class DiscordCommander:
    def __init__(self) -> None:
        self._token      = getattr(config, "DISCORD_BOT_TOKEN",  "") or ""
        self._channel_id = getattr(config, "DISCORD_CHANNEL_ID", "") or ""
        self._enabled    = bool(self._token and self._channel_id)
        self._last_msg_id: str | None = None
        self._thread: threading.Thread | None = None

        # Injected by bot.py after construction
        self.get_state:     Callable[[], "DashboardState | None"] = lambda: None
        self.do_stop:       Callable[[], None]  = lambda: None
        self.do_pause:      Callable[[], None]  = lambda: None
        self.do_resume:     Callable[[], None]  = lambda: None
        self.do_live:       Callable[[], None]  = lambda: None
        self.do_dry:        Callable[[], None]  = lambda: None
        self.do_refactor:   Callable[[str], None] = lambda _: None

    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            logger.debug("[Discord] BOT_TOKEN or CHANNEL_ID not set — commander disabled")
            return
        # Seed last_msg_id so we don't replay old messages on startup
        self._seed_last_message()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="discord-cmd"
        )
        self._thread.start()
        logger.info("[Discord] Command listener started")

    # ------------------------------------------------------------------

    def send(self, text: str) -> None:
        """Send a message to the channel."""
        if not self._enabled:
            return
        try:
            requests.post(
                f"{DISCORD_API}/channels/{self._channel_id}/messages",
                headers={"Authorization": f"Bot {self._token}"},
                json={"content": text},
                timeout=10,
            )
        except Exception as exc:
            logger.debug(f"[Discord] send failed: {exc}")

    # ------------------------------------------------------------------

    def _seed_last_message(self) -> None:
        """Read the latest message ID so we only process new messages."""
        try:
            r = requests.get(
                f"{DISCORD_API}/channels/{self._channel_id}/messages",
                headers={"Authorization": f"Bot {self._token}"},
                params={"limit": 1},
                timeout=10,
            )
            msgs = r.json()
            if msgs and isinstance(msgs, list):
                self._last_msg_id = msgs[0]["id"]
        except Exception:
            pass

    def _poll_loop(self) -> None:
        while True:
            try:
                self._check_messages()
            except Exception as exc:
                logger.debug(f"[Discord] poll error: {exc}")
            time.sleep(POLL_INTERVAL)

    def _check_messages(self) -> None:
        params: dict = {"limit": 10}
        if self._last_msg_id:
            params["after"] = self._last_msg_id

        r = requests.get(
            f"{DISCORD_API}/channels/{self._channel_id}/messages",
            headers={"Authorization": f"Bot {self._token}"},
            params=params,
            timeout=10,
        )
        if r.status_code != 200:
            return

        msgs = r.json()
        if not msgs or not isinstance(msgs, list):
            return

        # Process oldest first
        for msg in reversed(msgs):
            msg_id   = msg.get("id", "")
            content  = msg.get("content", "").strip()
            author   = msg.get("author", {})
            is_bot   = author.get("bot", False)

            # Update cursor
            if msg_id > (self._last_msg_id or ""):
                self._last_msg_id = msg_id

            # Ignore bot messages and non-commands
            if is_bot or not content.startswith("!"):
                continue

            self._handle(content)

    def _handle(self, content: str) -> None:
        parts   = content.split(None, 1)
        cmd     = parts[0].lower()
        arg     = parts[1].strip() if len(parts) > 1 else ""

        logger.info(f"[Discord] Command: {content}")

        if cmd == "!status":
            self.send(self._status_text())

        elif cmd == "!stop":
            self.send("⏹️ Stopping bot...")
            self.do_stop()

        elif cmd == "!pause":
            self.do_pause()
            self.send("⏸️ Trading paused — positions still managed.")

        elif cmd == "!resume":
            self.do_resume()
            self.send("▶️ Trading resumed.")

        elif cmd == "!live":
            self.do_live()
            self.send("🟢 Switched to LIVE trading.")

        elif cmd == "!dry":
            self.do_dry()
            self.send("🟡 Switched to DRY-RUN.")

        elif cmd == "!refactor":
            if not arg:
                self.send("Usage: `!refactor <description of what to improve>`")
                return
            self.send(f"🤖 Queuing LLM refactor: _{arg}_")
            self.do_refactor(arg)

        elif cmd == "!help":
            self.send(
                "**MIDUSBOT Commands**\n"
                "`!status` — current state\n"
                "`!pause` / `!resume` — pause/resume new trades\n"
                "`!live` / `!dry` — switch trading mode\n"
                "`!stop` — graceful shutdown\n"
                "`!refactor <text>` — trigger LLM analyst\n"
            )
        else:
            self.send(f"Unknown command `{cmd}`. Type `!help` for a list.")

    def _status_text(self) -> str:
        s = self.get_state()
        if s is None:
            return "⚠️ Bot state unavailable."

        mode     = "DRY-RUN" if config.DRY_RUN else "LIVE"
        paused   = " (PAUSED)" if config.TRADING_PAUSED else ""
        balance  = s.wallet_balance or s.balance
        wr       = f"{s.win_rate:.1%}" if s.total_trades else "—"
        pnl      = f"{s.total_pnl:+.2f}"
        daily    = f"{s.daily_pnl:+.2f}"
        n_pos    = len(s.positions)
        exposure = s.exposure

        lines = [
            f"**MIDUSBOT Status**",
            f"Mode: **{mode}**{paused}",
            f"Balance: **${balance:.2f}** USDC",
            f"Exposure: ${exposure:.2f}",
            f"Open positions: {n_pos}",
            f"Win rate: {wr}  ({s.wins}W / {s.total_trades}T)",
            f"Total P&L: ${pnl}",
            f"Daily P&L: ${daily}",
            f"Loop: #{s.loop_count}  Uptime: {s.uptime}",
        ]
        return "\n".join(lines)


# Singleton
commander = DiscordCommander()
