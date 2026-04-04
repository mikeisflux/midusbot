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

import json
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from src.client import Market, PolymarketClient
from src.dashboard import Dashboard, DashboardState
from src.feeds import BinanceWSFeed
from src.learner import AdaptiveLearner
from src.risk import RiskManager
from src.strategy import (
    UpDownMomentumStrategy,
    TrendFollowStrategy,
    _fetch_price,
    _detect_updown_market,
    _updown_window_mins,
    _market_seconds_into_window,
)
from src.trend import TrendTracker
from src.sim import SimPortfolio
from src.utils import alerter
from src.discord_bot import commander
from src.bot_positions import PositionsMixin
from src.bot_sim import SimMixin
from src.bot_scanner import ScannerMixin
import config
import src.webui as webui

_ERROR_LOG = Path("data/bot_errors.jsonl")


def _record_error(msg: str) -> None:
    try:
        _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ERROR_LOG.open("a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "error": msg}) + "\n")
    except Exception:
        pass


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


class PolymarketBot(ScannerMixin, SimMixin, PositionsMixin):
    def __init__(self, *, dashboard_enabled: bool = True) -> None:
        self._learner       = AdaptiveLearner(name="main")
        self._client        = PolymarketClient()
        self._updown        = UpDownMomentumStrategy()
        self._trend_tracker = TrendTracker()
        self._trend         = TrendFollowStrategy(self._trend_tracker)
        self._positions: dict[str, OpenPosition] = {}
        self._risk          = RiskManager(params=self._learner.risk_params, positions=self._positions)
        self._dashboard     = Dashboard(enabled=dashboard_enabled)
        self._dash_state    = DashboardState()

        self._running = False
        self._last_mode_switch: float = 0.0
        self._closed_market_ids: set[str] = set()

        self._price_feed = BinanceWSFeed()
        self._sim = SimPortfolio()

        self._sim_queue_path = Path("data/sim_queue.json")
        self._sim_queue: list[dict] = self._load_sim_queue()

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def run(self) -> None:
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

        webui.start(self._dash_state, port=8080, learner=self._learner,
                    close_position_fn=self._close_position, sim=self._sim)

        commander.get_state    = lambda: self._dash_state
        commander.get_sim      = lambda: self._sim
        commander.do_stop      = lambda: setattr(self, "_running", False)
        commander.do_pause     = lambda: setattr(config, "TRADING_PAUSED", True)
        commander.do_resume    = lambda: setattr(config, "TRADING_PAUSED", False)
        commander.do_live      = lambda: self._set_mode(dry_run=False, reason="Discord command")
        commander.do_dry       = lambda: self._set_mode(dry_run=True,  reason="Discord command")
        commander.do_refactor  = self._discord_refactor
        commander.do_reset_dry = self._reset_sim_wallet
        commander.start()

        if not config.DRY_RUN:
            stale = self._client.get_open_orders()
            if stale:
                logger.info(f"Cancelling {len(stale)} stale open order(s) from previous session...")
                self._client.cancel_all_orders()

        self._positions.clear()
        _pos_file = Path("data/positions.json")
        if _pos_file.exists():
            _pos_file.unlink()
            logger.info("Cleared stale positions file — will re-reconcile from CLOB.")

        if not config.DRY_RUN:
            self._reconcile_positions()

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

    def _loop_once(self) -> None:
        self._dash_state.loop_count += 1
        t0 = time.time()
        logger.info(f"-- Loop #{self._dash_state.loop_count} --")

        today = datetime.now().date()
        if not hasattr(self, "_today"):
            self._today = today
        if today != self._today:
            self._today = today
            self._risk.reset_daily()
            logger.info("Daily risk reset — new trading day started.")

        if not config.DRY_RUN or self._dash_state.loop_count % 10 == 0:
            self._sync_wallet_balance()

        if not config.DRY_RUN and self._dash_state.loop_count % 20 == 0:
            self._reconcile_positions()

        self._process_sim_queue()
        self._manage_positions()

        for sym in ("BTC", "XRP", "ETH", "SOL", "DOGE", "BNB", "HYPE"):
            _fetch_price(sym)

        if self._dash_state.loop_count % 4 == 1:
            from src.signals import _PRICE_CACHE, _PRICE_HISTORY
            import time as _time
            _now = _time.time()
            parts = []
            for sym in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
                cached = _PRICE_CACHE.get(sym)
                hist   = _PRICE_HISTORY.get(sym, [])
                ticks  = len(hist)
                span   = int(hist[-1][1] - hist[0][1]) if len(hist) >= 2 else 0
                price_str = f"${cached[0]:,.2f}" if cached else "-"
                status = f"✓{ticks}t/{span}s" if ticks >= 5 and span >= 90 else f"⏳{ticks}t/{span}s"
                parts.append(f"{sym}={price_str}({status})")
            logger.info("Feed: " + "  ".join(parts))

        self._dash_state.add_exec_log("scan",
            f"Orderbook depth scan — evaluating {self._dash_state.markets_scanned or '...'} markets")

        markets = self._client.get_markets()
        updown  = self._client.get_updown_markets()

        seen_ids    = {m.id for m in updown}
        all_markets = updown + [m for m in markets if m.id not in seen_ids]
        candidates  = self._filter_markets(all_markets)
        self._dash_state.markets_scanned = len(all_markets)
        self._dash_state.candidates = len(candidates)
        updown_5m = [
            m for m in candidates
            if _detect_updown_market(m.question) and _updown_window_mins(m.question) == 5
        ]
        logger.info(
            f"{len(candidates)}/{len(all_markets)} markets pass filters — "
            f"{len(updown_5m)} tradeable 5-min UpDown."
        )

        self._dash_state.add_exec_log("scan",
            f"Evaluating {len(candidates)} candidate markets on CLOB...")

        signals_found = 0
        trades_placed = 0
        _updown_bet_this_loop: set[str] = set()
        _updown_assets_held:   set[str] = set()
        for pos in self._positions.values():
            sym = _detect_updown_market(pos.question)
            if sym:
                _updown_assets_held.add(sym)

        for market in candidates:
            if not self._running:
                break

            _asset = _detect_updown_market(market.question)
            if _asset is None:
                continue

            if _updown_window_mins(market.question) != 5:
                continue

            if self._already_positioned(market):
                continue
            if (market.yes_token.token_id in self._positions or
                    market.no_token.token_id in self._positions):
                continue
            if _asset in _updown_bet_this_loop or _asset in _updown_assets_held:
                continue

            _secs = _market_seconds_into_window(market)
            if _secs is None or _secs < 5 or _secs > 240:
                continue

            ob = self._client.get_order_book(market.yes_token.token_id)

            try:
                sig = self._trend.analyse(market, ob)
            except Exception as _e:
                logger.warning(f"[BOT] trend.analyse error ({market.question[:40]}): {_e}")
                _record_error(str(_e))
                continue

            if sig is None:
                try:
                    sig = self._updown.analyse(market, ob)
                except Exception as _e:
                    logger.warning(f"[BOT] updown.analyse error ({market.question[:40]}): {_e}")
                    _record_error(str(_e))
                    continue

            if sig is None:
                continue

            if self._already_positioned(market, token_id=sig.token_id):
                continue

            hours_to_close = None
            if market.end_date:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    _end  = _dt.fromisoformat(market.end_date.replace("Z", "+00:00"))
                    _secs = (_end - _dt.now(_tz.utc)).total_seconds()
                    hours_to_close = max(0.0, _secs / 3600)
                except Exception:
                    pass
            sig.hours_to_close = hours_to_close
            if ob:
                sig.best_ask = ob.best_ask
                sig.best_bid = ob.best_bid
            if sig.side == "NO" and market.no_token:
                try:
                    no_ob = self._client.get_order_book(market.no_token.token_id)
                    if no_ob and no_ob.best_ask < 0.99:
                        sig.no_best_ask = no_ob.best_ask
                except Exception:
                    pass

            signals_found += 1
            self._dash_state.push_signal(sig)

            if self._execute_signal(sig):
                trades_placed += 1
                if _asset:
                    _updown_bet_this_loop.add(_asset)
                    _updown_assets_held.add(_asset)

        self._dash_state.scan_latency_ms = int((time.time() - t0) * 1000)
        self._dash_state.exposure        = self._risk.total_exposure()

        if signals_found == 0 and len(candidates) > 0:
            logger.debug("No trades this loop — signal/edge threshold not met")
        elif signals_found > 0 and trades_placed == 0:
            logger.debug("Signals found but cap/guards blocked all trades")

        logger.info(
            f"-- Loop done — signals={signals_found}  trades={trades_placed}  "
            f"exposure=${self._risk.total_exposure():.2f} --"
        )

    def _refresh_dashboard(self) -> None:
        self._dash_state.learned   = self._learner.get_dashboard_dict()
        self._dash_state.sim_stats = self._sim.get_stats()
        self._dashboard.refresh(self._dash_state)

    @staticmethod
    def _secs_until_next_window(window_mins: int = 5) -> float:
        now = time.time()
        window_secs = window_mins * 60
        return window_secs - (now % window_secs)

    def _smart_sleep(self) -> None:
        BURST_LEAD_SECS     = 6
        BURST_LOOPS         = 4
        BURST_INTERVAL_SECS = 1.5

        secs_to_boundary = self._secs_until_next_window(5)

        if secs_to_boundary <= BURST_LEAD_SECS:
            wait = max(0.05, secs_to_boundary - 0.1)
            logger.debug(
                f"[BURST] Window opens in {secs_to_boundary:.1f}s — "
                f"sleeping {wait:.1f}s then firing {BURST_LOOPS} rapid loops"
            )
            time.sleep(wait)
            for i in range(BURST_LOOPS):
                if not self._running:
                    break
                try:
                    self._loop_once()
                except Exception as exc:
                    logger.exception(f"Unhandled error in burst loop {i}: {exc}")
                self._refresh_dashboard()
                if i < BURST_LOOPS - 1:
                    time.sleep(BURST_INTERVAL_SECS)
        else:
            time.sleep(config.LOOP_INTERVAL_SECONDS)

    def _discord_refactor(self, instruction: str) -> None:
        def _run():
            try:
                from src.analyst import analyse_and_update, load_params, save_params
                params = load_params()
                params["_discord_instruction"] = instruction
                save_params(params)
                analyse_and_update(self._learner)
                commander.send("✅ Refactor complete.")
            except Exception as exc:
                commander.send(f"❌ Refactor failed: {exc}")
        threading.Thread(target=_run, daemon=True, name="discord-refactor").start()

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received...")
        self._running = False
        self._price_feed.stop()
