"""
Bot orchestrator — ties together client, strategies, risk manager,
adaptive learner, and live dashboard.

Loop per iteration
──────────────────
1. Manage existing positions (stop-loss / take-profit).
2. Scan UpDown crypto markets — run TrendFollow then Momentum strategy.
3. Size and place orders for valid signals.
4. Record trades in the learner; learner adapts when enough data exists.
5. Push state to the dashboard.
6. Sleep until next iteration.
"""
from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from src.client import PolymarketClient
from src.dashboard import Dashboard, DashboardState
from src.feeds import BinanceWSFeed
from src.learner import AdaptiveLearner
from src.risk import RiskManager
from src.strategy import (
    UpDownMomentumStrategy,
    TrendFollowStrategy,
)
from src.trend import TrendTracker
from src.sim import SimPortfolio
from src.utils import alerter
from src.discord_bot import commander
from src.positions import PositionsMixin
from src.bot_sim import SimMixin
from src.scanner import ScannerMixin
from src.market_maker import MarketMakerMixin
import config
import src.webui as webui


def _check_clock_sync() -> None:
    """
    Verify server clock is within 500ms of NTP time.
    Window boundary detection is wrong if clock is off — this causes us to
    enter markets at the wrong time or miss the oracle lag window entirely.
    """
    try:
        import requests as _req
        # Use Binance server time as reference (no external NTP library needed)
        t0 = time.time()
        resp = _req.get("https://api.binance.com/api/v3/time", timeout=3)
        rtt  = time.time() - t0
        server_ms = resp.json()["serverTime"]
        server_ts = server_ms / 1000.0
        local_ts  = t0 + rtt / 2   # adjust for network RTT
        drift_ms  = abs(local_ts - server_ts) * 1000
        if drift_ms > 500:
            logger.warning(
                f"[CLOCK] Server clock drift {drift_ms:.0f}ms vs Binance NTP — "
                f"window boundary detection may be off! Check system NTP sync."
            )
        else:
            logger.info(f"[CLOCK] Clock sync OK — drift {drift_ms:.0f}ms vs Binance")
    except Exception as exc:
        logger.debug(f"[CLOCK] Clock check failed: {exc}")


def _audit_api_latency() -> None:
    """
    Measure round-trip latency to Binance and Polymarket APIs at startup.

    Binance REST latency does NOT affect signal quality — prices come from
    the WebSocket feed which is already connected by the time this runs.
    Binance REST is only used for initial price-history loading and fallback.

    Polymarket latency (Gamma + CLOB) directly affects order execution speed,
    so those thresholds are stricter.
    """
    import requests as _req
    # (name, url, warn_above_ms, note_if_high)
    endpoints = [
        (
            "Binance",
            "https://api.binance.com/api/v3/ping",
            500,
            "REST used for price-history bootstrap only — WebSocket already streaming",
        ),
        (
            "Poly-Gamma",
            "https://gamma-api.polymarket.com/markets?limit=1",
            200,
            "affects market scanning speed",
        ),
        (
            "Poly-CLOB",
            "https://clob.polymarket.com/",
            200,
            "affects order execution speed — relocate server if consistently >200ms",
        ),
    ]
    for name, url, warn_ms, note in endpoints:
        try:
            t0  = time.time()
            _req.get(url, timeout=5)
            rtt = int((time.time() - t0) * 1000)
            if rtt > warn_ms:
                logger.warning(f"[LATENCY-AUDIT] {name} RTT={rtt}ms (>{warn_ms}ms) — {note}")
            else:
                logger.info(f"[LATENCY-AUDIT] {name} RTT={rtt}ms ✓")
        except Exception as exc:
            logger.debug(f"[LATENCY-AUDIT] {name} unreachable: {exc}")



@dataclass
class OpenPosition:
    market_id: str
    question: str
    token_id: str
    side: str
    shares: float
    entry_price: float
    cost_usdc: float
    momentum_signal: float = 0.0
    imbalance_signal: float = 0.0
    composite_signal: float = 0.0
    confidence: str = "LOW"
    order_id: Optional[str] = None
    is_external: bool = False
    entry_time: float = 0.0   # unix timestamp for time-based exits
    high_water_mark: float = 0.0  # highest price seen since entry (trailing stop)
    strategy: str = "momentum"    # "momentum" | "chainlink" | "arb" | "mm"
    sell_at_ts: float = 0.0       # if > 0, sell this position at or after this unix timestamp


class PolymarketBot(ScannerMixin, SimMixin, PositionsMixin, MarketMakerMixin):
    def __init__(self, *, dashboard_enabled: bool = True) -> None:
        self._learner       = AdaptiveLearner(name="main")
        self._client        = PolymarketClient()
        self._updown        = UpDownMomentumStrategy()
        self._trend_tracker = TrendTracker()
        self._trend         = TrendFollowStrategy(self._trend_tracker)
        # Wire session_tracker window-close events into TrendTracker so streaks
        # update every 5 min from actual Binance price direction, not just on
        # position closes. Without this, streaks freeze when the bot isn't trading.
        import src.session_tracker as _st
        _st.register_window_close_callback(self._trend_tracker.auto_record_direction)
        self._mm_orders_init()   # market maker order tracking
        self._positions: dict[str, OpenPosition] = {}
        self._risk          = RiskManager(params=self._learner.risk_params, positions=self._positions)
        self._risk._learner_ref = self._learner   # MC sizing reads journal via this ref
        self._dashboard     = Dashboard(enabled=dashboard_enabled)
        self._dash_state    = DashboardState()

        self._running = False
        self._last_mode_switch: float = 0.0
        from src.positions import _load_closed_market_ids
        self._closed_market_ids: set[str] = _load_closed_market_ids()

        self._price_feed = BinanceWSFeed()
        self._sim = SimPortfolio()

        self._sim_queue_path = Path("data/sim_queue.json")
        self._sim_queue: list[dict] = self._load_sim_queue()

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def run(self) -> None:
        # Clock sync + geographic latency audit on startup
        _check_clock_sync()
        _audit_api_latency()

        mode = "[DRY-RUN]" if config.DRY_RUN else "[LIVE]"
        logger.info(f"Polymarket bot starting — {mode}")
        logger.info(
            f"MAX_POS=${config.MAX_POSITION_USDC}  "
            f"MAX_EXPOSURE=${config.MAX_TOTAL_EXPOSURE_USDC}  "
            f"MIN_EDGE={config.MIN_EDGE:.1%}  "
            f"LOOP={config.LOOP_INTERVAL_SECONDS}s"
        )

        self._running = True
        self._dashboard.start()
        self._price_feed.start()
        self._warmup_until = time.time() + 90  # block trading for 90s while price history warms up

        webui.start(self._dash_state, port=8080, learner=self._learner,
                    close_position_fn=self._close_position, sim=self._sim)

        commander.get_state    = lambda: self._dash_state
        commander.get_sim      = lambda: self._sim
        commander.do_stop      = lambda: setattr(self, "_running", False)
        commander.do_pause     = lambda: setattr(config, "TRADING_PAUSED", True)
        commander.do_resume    = lambda: setattr(config, "TRADING_PAUSED", False)
        commander.do_live      = lambda: self._set_mode(dry_run=False, reason="Discord command")
        commander.do_dry       = lambda: self._set_mode(dry_run=True,  reason="Discord command")
        commander.do_reset_dry = self._reset_sim_wallet
        commander.start()

        # Always cancel stale open orders, even in DRY_RUN mode.
        # Handles the common case where the user sets DRY_RUN=true in .env
        # and restarts — any live orders from the previous session must be cancelled.
        try:
            stale = self._client.get_open_orders()
            if stale:
                logger.info(f"Cancelling {len(stale)} stale open order(s) from previous session...")
                self._client.cancel_all_orders()
        except Exception as _exc:
            logger.debug(f"[STARTUP] Order cancel check skipped: {_exc}")

        self._positions.clear()
        _pos_file = Path("data/positions.json")
        if _pos_file.exists():
            _pos_file.unlink()
            logger.info("Cleared stale positions file — will re-reconcile from CLOB.")

        # Always reconcile positions from CLOB.
        # In DRY_RUN mode: if any live positions are found (left over from a previous
        # live session), close them with real orders before entering paper-trading mode.
        self._reconcile_positions()
        if config.DRY_RUN and self._positions:
            _live_count = len(self._positions)
            logger.warning(
                f"[DRY-RUN STARTUP] Found {_live_count} live position(s) on Polymarket — "
                f"closing all with real orders before entering dry-run mode."
            )
            alerter.send(
                f"DRY-RUN startup: closing {_live_count} live position(s) from previous session.",
                level="warning",
            )
            # Force DRY_RUN=False so sell calls send real orders.
            # Both sell_via_swaps and place_limit_order short-circuit when DRY_RUN=True.
            config.DRY_RUN = False
            for _tok in list(self._positions.keys()):
                try:
                    self._close_position(_tok)
                except Exception as _exc:
                    logger.error(f"[DRY-RUN STARTUP] Could not close {_tok[:16]}: {_exc}")
            config.DRY_RUN = True
            self._positions.clear()

        for token_id in list(self._sim._open.keys()):
            if not any(s["token_id"] == token_id for s in self._sim_queue):
                logger.info(f"[SIM] Dropping orphaned open position {token_id[:16]}...")
                self._sim._open.pop(token_id, None)
        self._sim._save()

        active_tokens = set(self._positions.keys())
        stale_count = 0
        import time as _time
        for rec in self._learner.journal:
            if not rec.closed and rec.token_id not in active_tokens:
                rec.closed     = True
                rec.exit_price = rec.entry_price
                rec.closed_at  = _time.time()
                rec.pnl_usdc   = 0.0
                rec.pnl_pct    = 0.0
                stale_count   += 1
        if stale_count:
            self._learner._save_journal()
            logger.info(f"Purged {stale_count} stale open journal entries (positions not reconciled).")

        self._dash_state.restore_from_journal(self._learner.journal)
        self._sync_wallet_balance()
        self._dash_state.add_equity_point()
        self._start_news_arb_fast_monitor()
        self._dash_state.add_exec_log("info", "MIDUSBOT started — scanning Polymarket CLOB...")
        self._dash_state.add_exec_log("info",
            f"Config: MAX_POS=${config.MAX_POSITION_USDC}  "
            f"MIN_EDGE={config.MIN_EDGE:.1%}  "
            f"{'DRY-RUN simulation active' if config.DRY_RUN else 'LIVE TRADING'}")

        try:
            while self._running:
                self._check_trading_hours()
                if not self._running:
                    break

                try:
                    self._loop_once()
                except Exception as exc:
                    logger.exception(f"Unhandled error: {exc}")
                    alerter.send(f"Unhandled error in loop: `{exc}`", level="warning")

                self._refresh_dashboard()
                self._check_performance_guard()
                self._check_daily_loss_alert()

                if self._running:
                    self._smart_sleep()
        finally:
            self._dashboard.stop()
            alerter.send("Bot stopped.", level="warning", blocking=True)

        logger.info("Bot stopped.")

    _LIVE_FLOOR_WIN_RATE  = 0.40
    _LIVE_MIN_TRADES      = 30
    _DRY_RECOVER_WIN_RATE = 0.55
    _DRY_MIN_TRADES       = 20
    _SWITCH_COOLDOWN_SECS = 1800
    _EVAL_WINDOW          = 30

    def _recent_win_rate(self, dry_run: bool) -> tuple[int, float]:
        journal = getattr(self._learner, "_journal", [])
        trades  = [r for r in journal if r.closed and r.dry_run == dry_run]
        recent  = trades[-self._EVAL_WINDOW:]
        if not recent:
            return 0, 0.0
        wins = sum(1 for r in recent if r.pnl_usdc > 0)
        return len(recent), wins / len(recent)

    def _set_mode(self, dry_run: bool, reason: str) -> None:
        """Switch between live and dry-run mode safely.

        LIVE → DRY-RUN: close all open live positions at market before switching
                        so real USDC isn't left unmanaged on Polymarket.
        DRY-RUN → LIVE: discard pending sim trades (they're not real orders)
                        and reconcile real positions from CLOB.
        """
        if dry_run == config.DRY_RUN:
            return  # already in requested mode, nothing to do

        # ── LIVE → DRY-RUN: liquidate real open positions first ──────────
        if dry_run and self._positions:
            open_count = len(self._positions)
            logger.warning(
                f"[MODE-SWITCH] Switching to DRY-RUN with {open_count} open live position(s) — "
                f"closing all at market to protect real capital."
            )
            self._dash_state.add_exec_log(
                "warning",
                f"LIVE→DRY-RUN: closing {open_count} real position(s) before switching"
            )
            # Temporarily force DRY_RUN=False so sell_via_swaps / place_limit_order
            # send REAL orders. Both functions short-circuit when DRY_RUN=True.
            config.DRY_RUN = False
            for token_id in list(self._positions.keys()):
                try:
                    self._close_position(token_id)
                except Exception as exc:
                    logger.error(f"[MODE-SWITCH] Could not close {token_id[:16]}… : {exc}")
            config.DRY_RUN = True   # will be re-set below — be explicit

        # ── DRY-RUN → LIVE: discard sim state, reload real positions ─────
        if not dry_run:
            sim_count = len(getattr(self._sim, "_open", {}))
            if sim_count:
                logger.info(
                    f"[MODE-SWITCH] Discarding {sim_count} pending sim trade(s) — "
                    f"these were paper-only and have no corresponding CLOB orders."
                )
                self._sim._open.clear()
                self._sim_queue.clear()
                self._save_sim_queue()
                self._sim._save()

            # Re-reconcile so bot picks up any real open positions from Polymarket
            logger.info("[MODE-SWITCH] Re-reconciling positions from CLOB after switch to LIVE…")
            try:
                self._positions.clear()
                self._reconcile_positions()
            except Exception as exc:
                logger.warning(f"[MODE-SWITCH] Reconcile failed (positions may be stale): {exc}")

        config.DRY_RUN = dry_run
        try:
            env_path = Path(".env")
            lines = env_path.read_text().splitlines() if env_path.exists() else []
            new_lines, found = [], False
            for line in lines:
                if line.startswith("DRY_RUN="):
                    new_lines.append(f"DRY_RUN={'true' if dry_run else 'false'}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"DRY_RUN={'true' if dry_run else 'false'}")
            env_path.write_text("\n".join(new_lines) + "\n")
        except Exception as exc:
            logger.warning(f"[GUARD] Could not persist .env: {exc}")
        self._last_mode_switch = time.time()
        mode = "DRY-RUN" if dry_run else "LIVE"
        msg  = f"[AUTO-SWITCH] -> {mode}  reason: {reason}"
        logger.warning(msg)
        self._dash_state.add_exec_log("info", msg)
        alerter.send(
            f"Mode switched to *{mode}*\nReason: {reason}\n"
            f"Balance: ${self._dash_state.wallet_balance:.2f} USDC",
            level="warning" if dry_run else "info",
        )

    def _check_performance_guard(self) -> None:
        if time.time() - self._last_mode_switch < self._SWITCH_COOLDOWN_SECS:
            return
        if not config.DRY_RUN:
            count, wr = self._recent_win_rate(dry_run=False)
            if count >= self._LIVE_MIN_TRADES and wr < self._LIVE_FLOOR_WIN_RATE:
                self._set_mode(True,
                    f"live win rate {wr:.1%} < {self._LIVE_FLOOR_WIN_RATE:.1%} over {count} trades")
        else:
            count, wr = self._recent_win_rate(dry_run=True)
            if count >= self._DRY_MIN_TRADES and wr >= self._DRY_RECOVER_WIN_RATE:
                self._set_mode(False,
                    f"dry-run win rate {wr:.1%} >= {self._DRY_RECOVER_WIN_RATE:.1%} over {count} trades — going live")

    def _refresh_dashboard(self) -> None:
        self._dash_state.learned   = self._learner.get_dashboard_dict()
        self._dash_state.sim_stats = self._sim.get_stats()
        self._dashboard.refresh(self._dash_state)

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received...")
        self._running = False
        self._price_feed.stop()
