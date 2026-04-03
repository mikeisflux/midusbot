"""
Discord two-way command interface for MIDUSBOT.

Uses the Discord Gateway (WebSocket) so the bot appears Online and receives
messages in real-time — no polling delay, no offline status.

Requires in .env:
  DISCORD_BOT_TOKEN   — bot token from Discord Developer Portal
  DISCORD_CHANNEL_ID  — ID of the channel to listen/respond in

Also requires in Discord Developer Portal → Bot:
  ✓ Message Content Intent  (Privileged Gateway Intents)
  ✓ Server Members Intent   (optional but harmless)

Commands:
  !status    — mode, balance, open positions, win rate, P&L
  !stop      — graceful shutdown
  !pause     — pause new trades (keeps managing positions)
  !resume    — resume trading
  !live      — switch to live trading
  !dry       — switch to dry-run
  !refactor <text> — trigger LLM analyst with a custom instruction
  !help      — list commands

Runs as a daemon thread — never blocks the trading loop.
"""
from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Callable

import requests
import websocket
from loguru import logger

import config

if TYPE_CHECKING:
    from src.dashboard import DashboardState

GATEWAY_URL   = "wss://gateway.discord.gg/?v=10&encoding=json"
DISCORD_API   = "https://discord.com/api/v10"

# Gateway intents: GUILD_MESSAGES (1<<9) + MESSAGE_CONTENT (1<<15)
INTENTS = (1 << 9) | (1 << 15)   # 512 + 32768 = 33280


class DiscordCommander:
    def __init__(self) -> None:
        self._token      = getattr(config, "DISCORD_BOT_TOKEN",  "") or ""
        self._channel_id = getattr(config, "DISCORD_CHANNEL_ID", "") or ""
        self._enabled    = bool(self._token and self._channel_id)

        self._ws: websocket.WebSocketApp | None = None
        self._hb_thread: threading.Thread | None = None
        self._hb_interval: float = 41.25   # seconds, updated on HELLO
        self._hb_seq: int | None = None
        self._connected = False

        # Injected by bot.py after construction
        self.get_state:   Callable[[], "DashboardState | None"] = lambda: None
        self.do_stop:     Callable[[], None]  = lambda: None
        self.do_pause:    Callable[[], None]  = lambda: None
        self.do_resume:   Callable[[], None]  = lambda: None
        self.do_live:     Callable[[], None]  = lambda: None
        self.do_dry:      Callable[[], None]  = lambda: None
        self.do_refactor: Callable[[str], None] = lambda _: None

    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            logger.debug("[Discord] BOT_TOKEN or CHANNEL_ID not set — commander disabled")
            return
        t = threading.Thread(target=self._gateway_loop, daemon=True, name="discord-gw")
        t.start()
        logger.info("[Discord] Gateway commander starting")

    # ------------------------------------------------------------------

    def send(self, text: str) -> None:
        """POST a message to the channel via REST (fire-and-forget)."""
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
    # Gateway connection loop (reconnects on any failure)
    # ------------------------------------------------------------------

    def _gateway_loop(self) -> None:
        backoff = 1
        while True:
            try:
                self._ws = websocket.WebSocketApp(
                    GATEWAY_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=0)   # we handle our own heartbeat
            except Exception as exc:
                logger.warning(f"[Discord] Gateway exception: {exc}")
            logger.debug(f"[Discord] Reconnecting in {backoff}s…")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        logger.debug("[Discord] Gateway connected")

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        logger.debug(f"[Discord] Gateway error: {error}")

    def _on_close(self, ws: websocket.WebSocketApp, code: int, reason: str) -> None:
        self._connected = False
        logger.debug(f"[Discord] Gateway closed: {code} {reason}")

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return

        op  = payload.get("op")
        seq = payload.get("s")
        if seq is not None:
            self._hb_seq = seq

        if op == 10:   # HELLO
            interval = payload["d"]["heartbeat_interval"] / 1000.0
            self._hb_interval = interval
            self._start_heartbeat(ws, interval)
            self._identify(ws)

        elif op == 11:  # Heartbeat ACK
            pass

        elif op == 1:   # Heartbeat request
            self._send_heartbeat(ws)

        elif op == 0:   # DISPATCH
            t = payload.get("t")
            if t == "READY":
                self._connected = True
                logger.info("[Discord] Gateway ready — bot is Online")
            elif t == "MESSAGE_CREATE":
                self._handle_message(payload.get("d", {}))

        elif op == 9:   # Invalid session
            logger.warning("[Discord] Invalid session, reconnecting…")
            time.sleep(2)
            ws.close()

        elif op == 7:   # Reconnect
            ws.close()

    # ------------------------------------------------------------------

    def _identify(self, ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({
            "op": 2,
            "d": {
                "token":   self._token,
                "intents": INTENTS,
                "properties": {
                    "os":      "linux",
                    "browser": "midusbot",
                    "device":  "midusbot",
                },
            },
        }))

    def _send_heartbeat(self, ws: websocket.WebSocketApp) -> None:
        try:
            ws.send(json.dumps({"op": 1, "d": self._hb_seq}))
        except Exception:
            pass

    def _start_heartbeat(self, ws: websocket.WebSocketApp, interval: float) -> None:
        def _beat() -> None:
            # Initial jitter: Discord recommends waiting interval * random before first beat
            time.sleep(interval * 0.5)
            while self._ws is ws:
                self._send_heartbeat(ws)
                time.sleep(interval)
        t = threading.Thread(target=_beat, daemon=True, name="discord-hb")
        t.start()
        self._hb_thread = t

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
        # Only care about messages in our channel
        if msg.get("channel_id") != self._channel_id:
            return
        author = msg.get("author", {})
        if author.get("bot"):
            return
        content = msg.get("content", "").strip()
        if not content.startswith("!"):
            return
        logger.info(f"[Discord] Command from {author.get('username')}: {content}")
        self._handle(content)

    def _handle(self, content: str) -> None:
        parts = content.split(None, 1)
        cmd   = parts[0].lower()
        arg   = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "!status":
            self.send(self._status_text())

        elif cmd == "!stop":
            self.send("⏹️ Stopping bot…")
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

    # ------------------------------------------------------------------

    def _status_text(self) -> str:
        s = self.get_state()
        if s is None:
            return "⚠️ Bot state unavailable."

        mode    = "DRY-RUN" if config.DRY_RUN else "LIVE"
        paused  = " (PAUSED)" if config.TRADING_PAUSED else ""
        balance = s.wallet_balance or s.balance
        wr      = f"{s.win_rate:.1%}" if s.total_trades else "—"
        pnl     = f"{s.total_pnl:+.2f}"
        daily   = f"{s.daily_pnl:+.2f}"

        lines = [
            "**MIDUSBOT Status**",
            f"Mode: **{mode}**{paused}",
            f"Balance: **${balance:.2f}** USDC",
            f"Exposure: ${s.exposure:.2f}",
            f"Open positions: {len(s.positions)}",
            f"Win rate: {wr}  ({s.wins}W / {s.total_trades}T)",
            f"Total P&L: ${pnl}",
            f"Daily P&L: ${daily}",
            f"Loop: #{s.loop_count}  Uptime: {s.uptime}",
        ]
        return "\n".join(lines)


# Singleton
commander = DiscordCommander()
