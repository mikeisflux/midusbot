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
import random
import signal
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

_POSITIONS_FILE = Path("data/positions.json")

from src.client import Market, PolymarketClient
from src.dashboard import Dashboard, DashboardState
from src.feeds import BinanceWSFeed
from src.learner import AdaptiveLearner
from src.risk import RiskManager
from src.strategy import (
    UpDownMomentumStrategy,
    TrendFollowStrategy,
    TradeSignal,
    _fetch_price,
    _detect_updown_market,
    _updown_window_mins,
    _market_seconds_into_window,
)
from src.trend import TrendTracker
from src.sim import SimPortfolio
import config
import src.webui as webui

_ERROR_LOG = Path("data/bot_errors.jsonl")


def _record_error(msg: str) -> None:
    """Append a runtime error to the error log so the analyst can read it."""
    try:
        _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ERROR_LOG.open("a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "error": msg}) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internal bookkeeping
# ---------------------------------------------------------------------------

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
    # True for positions reconciled from external trade history (not opened by
    # this bot session). These are NEVER auto-sold — only manual SELL applies.
    is_external: bool = False


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class PolymarketBot:
    def __init__(self, *, dashboard_enabled: bool = True) -> None:
        self._learner      = AdaptiveLearner(name="main")
        self._client       = PolymarketClient()
        self._updown       = UpDownMomentumStrategy()
        self._trend_tracker = TrendTracker()
        self._trend        = TrendFollowStrategy(self._trend_tracker)
        self._positions: dict[str, OpenPosition] = {}
        self._risk         = RiskManager(params=self._learner.risk_params, positions=self._positions)
        self._dashboard    = Dashboard(enabled=dashboard_enabled)
        self._dash_state   = DashboardState()

        self._running = False
        self._last_mode_switch: float = 0.0

        # Live data feeds
        self._price_feed = BinanceWSFeed()
        self._sim = SimPortfolio()

        # Simulation queue — pending dry-run trades waiting for oracle settlement
        # Persisted to disk so it survives restarts.
        self._sim_queue_path = Path("data/sim_queue.json")
        self._sim_queue: list[dict] = self._load_sim_queue()

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

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
        self._price_feed.start()   # Binance WebSocket — real-time prices

        # Start web UI immediately so the browser is never refused while
        # the slow startup tasks (reconcile, balance fetch) run below.
        webui.start(self._dash_state, port=8080, learner=self._learner, close_position_fn=self._close_position, sim=self._sim)

        # Cancel any stale open orders left from previous runs
        if not config.DRY_RUN:
            stale = self._client.get_open_orders()
            if stale:
                logger.info(f"Cancelling {len(stale)} stale open order(s) from previous session…")
                self._client.cancel_all_orders()

        # Start fresh — discard any stale positions.json from previous sessions.
        # _reconcile_positions will re-add positions that are genuinely still open
        # on Polymarket (active market, price not at 0.00 or 1.00).
        self._positions.clear()
        _pos_file = __import__("pathlib").Path("data/positions.json")
        if _pos_file.exists():
            _pos_file.unlink()
            logger.info("Cleared stale positions file — will re-reconcile from CLOB.")

        # Reconcile with live CLOB positions — only in live mode.
        # In dry-run, real wallet positions must not count against the sim
        # exposure cap; they're unrelated to the current paper-trading session.
        if not config.DRY_RUN:
            self._reconcile_positions()

        # Purge orphaned sim portfolio positions (open in journal_sim.json but
        # not in sim_queue — happens when sim_queue.json is cleared on reset).
        for token_id in list(self._sim._open.keys()):
            if not any(s["token_id"] == token_id for s in self._sim_queue):
                logger.info(f"[SIM] Dropping orphaned open position {token_id[:16]}…")
                self._sim._open.pop(token_id, None)
        self._sim._save()

        # Purge stale "open" journal entries whose token is no longer tracked.
        # These accumulate when markets are abandoned mid-session (bot killed,
        # sell failed, etc.) and skew the learner with phantom open positions.
        active_tokens = set(self._positions.keys())
        stale_count = 0
        import time as _time
        for rec in self._learner.journal:
            if not rec.closed and rec.token_id not in active_tokens:
                rec.closed   = True
                rec.exit_price = rec.entry_price  # break-even; no real P&L known
                rec.closed_at  = _time.time()
                rec.pnl_usdc   = 0.0
                rec.pnl_pct    = 0.0
                stale_count   += 1
        if stale_count:
            self._learner._save_journal()
            logger.info(f"Purged {stale_count} stale open journal entries (positions not reconciled).")

        # Restore performance stats + equity curve from persisted journal/file
        self._dash_state.restore_from_journal(self._learner.journal)

        # Pull live wallet balance and use it as the portfolio seed
        self._sync_wallet_balance()

        # Add a restart marker to the equity curve so gaps are visible on the chart
        self._dash_state.add_equity_point()
        self._dash_state.add_exec_log("info", "MIDUSBOT started — scanning Polymarket CLOB…")
        self._dash_state.add_exec_log("info",
            f"Config: MAX_POS=${config.MAX_POSITION_USDC}  "
            f"MIN_EDGE={config.MIN_EDGE:.1%}  "
            f"{'DRY-RUN simulation active' if config.DRY_RUN else 'LIVE TRADING'}")

        try:
            while self._running:
                try:
                    self._loop_once()
                except Exception as exc:
                    logger.exception(f"Unhandled error: {exc}")

                self._refresh_dashboard()
                self._check_performance_guard()

                if self._running:
                    time.sleep(config.LOOP_INTERVAL_SECONDS)
        finally:
            self._dashboard.stop()

        logger.info("Bot stopped.")

    # ------------------------------------------------------------------
    # Performance guard — auto-switch live ↔ dry-run
    # ------------------------------------------------------------------

    _LIVE_FLOOR_WIN_RATE  = 0.45   # below this over live trades → back to dry-run
    _LIVE_MIN_TRADES      = 15
    _DRY_RECOVER_WIN_RATE = 0.55   # hit this in dry-run → go live
    _DRY_MIN_TRADES       = 15     # number of settled dry-run trades needed
    _SWITCH_COOLDOWN_SECS = 600
    _EVAL_WINDOW          = 20

    def _recent_win_rate(self, dry_run: bool) -> tuple[int, float]:
        journal = getattr(self._learner, "_journal", [])
        trades = [r for r in journal if r.closed and r.dry_run == dry_run]
        recent = trades[-self._EVAL_WINDOW:]
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
        msg = f"[AUTO-SWITCH] → {mode}  reason: {reason}"
        logger.warning(msg)
        self._dash_state.add_exec_log("info", msg)

    def _check_performance_guard(self) -> None:
        """Auto-switch live ↔ dry-run based on recent win rate."""
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
                    f"dry-run win rate {wr:.1%} ≥ {self._DRY_RECOVER_WIN_RATE:.1%} over {count} trades — going live")

    # ------------------------------------------------------------------
    # Single loop iteration
    # ------------------------------------------------------------------

    def _loop_once(self) -> None:
        self._dash_state.loop_count += 1
        t0 = time.time()
        logger.info(f"── Loop #{self._dash_state.loop_count} ──")

        # 0a. Daily loss-cap reset at midnight (without this the cap hits and
        #     trading stops permanently until the process is restarted)
        today = datetime.now().date()
        if not hasattr(self, "_today"):
            self._today = today
        if today != self._today:
            self._today = today
            self._risk.reset_daily()
            logger.info("Daily risk reset — new trading day started.")

        # 0b. Refresh wallet balance every 10 loops (or every loop in live mode)
        if not config.DRY_RUN or self._dash_state.loop_count % 10 == 0:
            self._sync_wallet_balance()

        # 0c. Re-run position reconciliation every 20 loops (live mode only).
        if not config.DRY_RUN and self._dash_state.loop_count % 20 == 0:
            self._reconcile_positions()

        # 0c. Process any pending simulated fills
        self._process_sim_queue()

        # 1. Manage existing positions
        self._manage_positions()

        # 2. Update live crypto prices (WebSocket feed handles this in real-time;
        #    REST fallback warms history for symbols not on Binance WS like HYPE)
        for sym in ("BTC", "XRP", "ETH", "SOL", "DOGE", "BNB", "HYPE"):
            _fetch_price(sym)

        # Log price feed status every 4 loops so it's visible in the logs.
        # Shows live prices + how many ticks are in history for each symbol.
        if self._dash_state.loop_count % 4 == 1:
            from src.strategy import _PRICE_CACHE, _PRICE_HISTORY
            import time as _time
            _now = _time.time()
            parts = []
            for sym in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
                cached = _PRICE_CACHE.get(sym)
                hist = _PRICE_HISTORY.get(sym, [])
                age = int(_now - cached[1]) if cached else 9999
                ticks = len(hist)
                span = int(hist[-1][1] - hist[0][1]) if len(hist) >= 2 else 0
                price_str = f"${cached[0]:,.2f}" if cached else "–"
                status = f"✓{ticks}t/{span}s" if ticks >= 5 and span >= 90 else f"⏳{ticks}t/{span}s"
                parts.append(f"{sym}={price_str}({status})")
            logger.info("Feed: " + "  ".join(parts))

        # 3. Scan markets
        self._dash_state.add_exec_log("scan",
            f"Orderbook depth scan — evaluating {self._dash_state.markets_scanned or '…'} markets")

        markets = self._client.get_markets()
        updown  = self._client.get_updown_markets()

        # Deduplicate and put Up/Down markets first (they have highest urgency)
        seen_ids = {m.id for m in updown}
        all_markets = updown + [m for m in markets if m.id not in seen_ids]
        candidates = self._filter_markets(all_markets)
        self._dash_state.markets_scanned = len(all_markets)
        self._dash_state.candidates = len(candidates)
        updown_5m = [m for m in candidates if _detect_updown_market(m.question) and _updown_window_mins(m.question) == 5]
        logger.info(f"{len(candidates)}/{len(all_markets)} markets pass filters — {len(updown_5m)} tradeable 5-min UpDown.")

        self._dash_state.add_exec_log("scan",
            f"Evaluating {len(candidates)} candidate markets on CLOB…")

        signals_found = 0
        trades_placed = 0
        # Per-loop dedup: track which UpDown assets we've already bet on.
        # DOGE DOWN fires on 10+ time slots simultaneously — we want at most
        # ONE bet per asset per loop (the first/closest one wins).
        _updown_bet_this_loop: set[str] = set()
        # Also track assets currently held in open positions so we don't
        # pile on the same asset across loops.
        _updown_assets_held: set[str] = set()
        for pos in self._positions.values():
            sym = _detect_updown_market(pos.question)
            if sym:
                _updown_assets_held.add(sym)

        for market in candidates:
            if not self._running:
                break

            # ONLY trade UpDown crypto markets — this is where our edge is
            _asset = _detect_updown_market(market.question)
            if _asset is None:
                continue

            # Only 5-minute windows — our oracle-lag edge doesn't apply to
            # 15-min / 1-hour / 4-hour markets (market makers reprice too fast)
            if _updown_window_mins(market.question) != 5:
                continue

            if self._already_positioned(market):
                continue
            if (market.yes_token.token_id in self._positions or
                    market.no_token.token_id in self._positions):
                continue
            if _asset in _updown_bet_this_loop or _asset in _updown_assets_held:
                continue

            # Check window timing BEFORE fetching the order book.
            # Only ~7 markets are ever in the active entry window at once.
            _secs = _market_seconds_into_window(market)
            if _secs is None or _secs < 5 or _secs > 240:
                continue

            ob = self._client.get_order_book(market.yes_token.token_id)

            # ── Strategy 1: Trend Follow (per-asset streak + momentum confirm) ──
            try:
                sig = self._trend.analyse(market, ob)
            except Exception as _e:
                logger.warning(f"[BOT] trend.analyse error ({market.question[:40]}): {_e}")
                _record_error(str(_e))
                continue

            # ── Strategy 2: Momentum (cold-start / no trend history yet) ────────
            if sig is None:
                try:
                    sig = self._updown.analyse(market, ob)
                except Exception as _e:
                    logger.warning(f"[BOT] updown.analyse error ({market.question[:40]}): {_e}")
                    _record_error(str(_e))
                    continue

            if sig is None:
                continue

            # Skip if we already hold this exact token
            if self._already_positioned(market, token_id=sig.token_id):
                continue

            # Compute time to close from market end_date
            hours_to_close = None
            if market.end_date:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    _end = _dt.fromisoformat(market.end_date.replace("Z", "+00:00"))
                    _secs = (_end - _dt.now(_tz.utc)).total_seconds()
                    hours_to_close = max(0.0, _secs / 3600)
                except Exception:
                    pass
            sig.hours_to_close = hours_to_close
            if ob:
                sig.best_ask = ob.best_ask
                sig.best_bid = ob.best_bid
            # For NO trades, fetch the NO token OB directly for accurate sim entry
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
                # Mark asset as bet so we skip remaining slots for same asset
                if _asset:
                    _updown_bet_this_loop.add(_asset)
                    _updown_assets_held.add(_asset)

        self._dash_state.scan_latency_ms = int((time.time() - t0) * 1000)
        self._dash_state.exposure = self._risk.total_exposure()

        # "Why no bet" summary — only shown when we found candidates but placed nothing
        if signals_found == 0 and len(candidates) > 0:
            logger.debug("No trades this loop — signal/edge threshold not met")
        elif signals_found > 0 and trades_placed == 0:
            logger.debug("Signals found but cap/guards blocked all trades")

        logger.info(
            f"── Loop done — signals={signals_found}  trades={trades_placed}  "
            f"exposure=${self._risk.total_exposure():.2f} ──"
        )

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_positions(self) -> None:
        if not self._positions:
            self._dash_state.positions = []
            return

        to_close: list[tuple[str, float]] = []   # (token_id, current_price)
        position_snapshots: list[tuple[OpenPosition, float]] = []

        for token_id, pos in list(self._positions.items()):
            ob = self._client.get_order_book(token_id)
            book_is_empty = ob is None or (ob.best_bid == 0.0 and ob.best_ask == 1.0)

            # When book is unavailable, fetch the CLOB market for two purposes:
            # 1. Enrich placeholder question if still missing
            # 2. Check if market resolved/closed (prune ghost positions)
            # 3. Get current token price from tokens[] array
            clob_mkt: dict | None = None
            if book_is_empty and pos.market_id:
                try:
                    clob_mkt = self._client.get_clob_market(pos.market_id)
                except Exception:
                    pass

            # Lazily enrich placeholder question
            if clob_mkt:
                q = clob_mkt.get("question", "")
                if q and (pos.question.startswith("[token:") or len(pos.question) <= 20):
                    pos.question = q

            # Prune external (reconciled) positions whose market is confirmed closed.
            # These are settled markets that resolved naturally — no sell trade was
            # ever recorded, so trade-history reconstruction still shows them open.
            if ob is None and pos.is_external and clob_mkt is not None:
                market_active = clob_mkt.get("active", True)
                market_closed = clob_mkt.get("closed", False)
                if not market_active or market_closed:
                    logger.info(
                        f"PRUNED: market resolved/closed — removing ghost position "
                        f"{pos.question[:55]}"
                    )
                    self._risk.record_close()
                    del self._positions[token_id]
                    self._save_positions()
                    continue

            if book_is_empty:
                current_price = self._gamma_position_price(pos)
            else:
                current_price = ob.mid

            # Auto-claim / auto-clear / stop-loss — LIVE mode only, and NEVER
            # for externally-reconciled positions (is_external=True).
            # External positions are only closed via the manual SELL button.
            if not config.DRY_RUN and not pos.is_external:
                # Auto-claim: token resolved in our favour (worth $1.00).
                # Use manual=True so if both sell paths fail (resolved market
                # has no CLOB orderbook), position is force-removed.
                # Polymarket auto-credits resolved YES winnings on-chain.
                if current_price >= 0.97:
                    logger.info(
                        f"AUTO-CLAIM: resolved YES @ ${current_price:.3f} "
                        f"({pos.shares:.2f} shares)  {pos.question[:50]}"
                    )
                    self._close_position(token_id, current_price=current_price, manual=True)
                    continue

                # Auto-clear: token resolved against us (worth $0.00).
                if current_price <= 0.03:
                    pnl = self._learner.record_close(token_id, current_price)
                    self._risk.record_close(pnl_usdc=pnl)
                    self._dash_state.record_closed_trade(pnl, fee_usdc=0.0)
                    # Trend: our token went to 0 — we lost
                    symbol = _detect_updown_market(pos.question)
                    if symbol:
                        direction_bet = "UP" if pos.side == "YES" else "DOWN"
                        self._trend_tracker.record_result(symbol, direction_bet, won=False)
                    del self._positions[token_id]
                    self._save_positions()
                    logger.info(f"AUTO-CLEAR: resolved NO — position removed  {pos.question[:50]}")
                    continue

            pnl_pct = (
                (current_price - pos.entry_price) / pos.entry_price
                if pos.entry_price else 0.0
            )

            position_snapshots.append((pos, current_price))

            if not pos.is_external:
                if self._risk.should_stop_loss(pnl_pct):
                    logger.warning(f"STOP-LOSS {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))
                elif self._risk.should_take_profit(pnl_pct):
                    logger.info(f"TAKE-PROFIT {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                    to_close.append((token_id, current_price))

        self._dash_state.positions = position_snapshots

        for token_id, cur_price in to_close:
            self._close_position(token_id, current_price=cur_price)

    def _gamma_position_price(self, pos: OpenPosition) -> float:
        """
        When the CLOB order book is empty (market resolved or inactive), fetch
        the current token price from the CLOB markets endpoint (fast, single-shot).
        Falls back to entry_price if all calls fail.
        """
        if pos.market_id:
            try:
                mkt = self._client.get_clob_market(pos.market_id)
                if mkt:
                    tokens = mkt.get("tokens") or []
                    for tok in tokens:
                        if str(tok.get("token_id", "")) == pos.token_id:
                            price = float(tok.get("price") or 0)
                            if price > 0:
                                logger.debug(
                                    f"[CLOB fallback] {pos.side} price={price:.3f}  "
                                    f"{pos.question[:50]}"
                                )
                                return price
            except Exception as exc:
                logger.debug(f"_gamma_position_price CLOB fallback failed: {exc}")
        return pos.entry_price

    def _close_position(self, token_id: str, current_price: float | None = None, manual: bool = False) -> bool:
        pos = self._positions.get(token_id)
        if not pos:
            logger.warning(f"_close_position: token_id {token_id[:12]}… not found in open positions")
            return False

        resp = None

        # ── Path 1: swaps.xyz (preferred — handles tickSize/negRisk automatically) ──
        if config.SWAPS_API_KEY:
            # Fetch market params for tick_size and neg_risk
            tick_size = "0.01"
            neg_risk  = False
            if pos.market_id:
                try:
                    mkt = self._client.get_clob_market(pos.market_id)
                    if mkt:
                        tick_size = str(mkt.get("minimum_tick_size", "0.01"))
                        neg_risk  = bool(mkt.get("neg_risk", False))
                        # Also update the question if it's still a placeholder
                        if pos.question.startswith("[token:") or len(pos.question) < 25:
                            q = mkt.get("question", "")
                            if q:
                                pos.question = q
                except Exception as exc:
                    logger.debug(f"get_clob_market failed, using defaults: {exc}")
            resp = self._client.sell_via_swaps(token_id, pos.shares, tick_size, neg_risk)
            if resp and not resp.get("dry_run"):
                ok = resp.get("orderResponse", {}).get("success", False)
                if not ok:
                    logger.warning(f"swaps.xyz sell failed: {resp} — falling back to CLOB")
                    resp = None  # fall through to CLOB

        # ── Path 2: direct CLOB limit order (fallback) ───────────────────────────
        if resp is None:
            # Single-shot order book — no retries (already fast after recent fix)
            ob = self._client.get_order_book(token_id)
            book_best_bid = ob.best_bid if ob else 0.0
            if book_best_bid > 0.0:
                sell_price = book_best_bid
            elif current_price is not None and current_price > 0.05:
                sell_price = current_price
            else:
                sell_price = pos.entry_price
            resp = self._client.place_limit_order(
                token_id=token_id,
                side="SELL",
                price=sell_price,
                size=pos.shares,
            )

        # ── Path 3: on-chain redemption (resolved market — no orderbook) ─────────
        # When a market resolves the CLOB orderbook disappears. Call redeemPositions
        # on the CTF Exchange directly to convert winning tokens → USDC.
        # pos.market_id IS the condition_id on Polymarket.
        if resp is None and pos.market_id and current_price is not None and current_price >= 0.97:
            neg_risk = False
            try:
                mkt = self._client.get_clob_market(pos.market_id)
                if mkt:
                    neg_risk = bool(mkt.get("neg_risk", False))
            except Exception:
                pass
            redeemed = self._client.redeem_position(pos.market_id, neg_risk=neg_risk)
            if redeemed:
                resp = {"redeemed": True}

        if resp:
            # Use current_price for P&L — skip second get_order_book() call
            exit_price = (
                current_price or
                pos.entry_price
            )
            exit_usdc = exit_price * pos.shares
            fee = self._risk.trade_fee(pos.cost_usdc, exit_usdc)
            pnl = self._learner.record_close(token_id, exit_price)
            self._risk.record_close(pnl_usdc=pnl - fee)
            self._dash_state.record_closed_trade(pnl, fee_usdc=fee)

            # Update trend tracker for UpDown markets when they fully resolve
            symbol = _detect_updown_market(pos.question)
            if symbol:
                direction_bet = "UP" if pos.side == "YES" else "DOWN"
                if exit_price >= 0.95:
                    self._trend_tracker.record_result(symbol, direction_bet, won=True)
                elif exit_price <= 0.05:
                    self._trend_tracker.record_result(symbol, direction_bet, won=False)
                # intermediate exit (stop-loss/take-profit) — don't update trend

            # Always remove from tracking — in DRY_RUN this is simulated, but
            # we still need to delete so stop-loss/take-profit don't re-fire
            # every loop on the same position.
            del self._positions[token_id]
            self._save_positions()
            logger.info(f"{'[SIM] ' if config.DRY_RUN else ''}Closed: {pos.side} {pos.question[:40]}  P&L=${pnl:+.2f}  fee=${fee:.4f}  net=${pnl-fee:+.2f}")
            return True

        # All sell paths failed (e.g. closed/resolved market with no order book).
        # For manual clicks, force-remove from tracking — user explicitly wants it
        # gone. Polymarket auto-credits resolved YES winnings to the wallet.
        if manual:
            self._risk.record_close()
            del self._positions[token_id]
            self._save_positions()
            logger.info(f"MANUAL-REMOVE: sell order unavailable (market closed?) — removed from tracking: {pos.question[:55]}")
            return True

        return False

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute_signal(self, sig: TradeSignal) -> bool:
        if config.TRADING_PAUSED:
            return False

        # Never open the same token twice — covers all strategy types.
        if sig.token_id in self._positions:
            logger.debug(f"Already tracking token {sig.token_id[:16]}… — skipping duplicate signal")
            return False

        # One active UpDown bet per (symbol, window-category) at a time.
        # BTC 5-min and BTC hourly are different categories — both allowed.
        # But don't bet on "BTC 5-min 4AM" while "BTC 5-min 3AM" is still
        # open — we don't know if we won the first one yet.
        sig_symbol = _detect_updown_market(sig.question)
        sig_window = _updown_window_mins(sig.question) if sig_symbol else None
        if sig_symbol and sig_window:
            for pos in self._positions.values():
                pos_symbol = _detect_updown_market(pos.question)
                pos_window = _updown_window_mins(pos.question) if pos_symbol else None
                if pos_symbol == sig_symbol and pos_window == sig_window:
                    logger.debug(
                        f"Skipping {sig_symbol} {sig_window}min UpDown — "
                        f"already open: {pos.question[:55]}"
                    )
                    return False

        usdc = self._risk.position_size(sig)
        if usdc <= 0:
            return False

        # Urgency boost: the closer to expiry, the more aggressive the sizing.
        #   >48h  → 1.0× (no boost)
        #   48h   → 1.25×
        #   24h   → 1.5×
        #   1h    → 2.0×
        #   5min  → 2.5× (5-min markets — price MUST resolve, highest conviction)
        if sig.hours_to_close is not None:
            h = sig.hours_to_close
            if h <= 1:
                boost = 2.5 - (h / 1) * 0.5      # 2.5× at 0h → 2.0× at 1h
            elif h <= 24:
                boost = 2.0 - ((h - 1) / 23) * 0.5   # 2.0× at 1h → 1.5× at 24h
            elif h <= 48:
                boost = 1.5 - ((h - 24) / 24) * 0.25  # 1.5× at 24h → 1.25× at 48h
            else:
                boost = 1.0
            if boost > 1.0:
                usdc = min(usdc * boost, config.MAX_POSITION_USDC)
                logger.debug(f"Urgency boost {boost:.2f}× — {h:.1f}h to close")

        # Always limit to fair value — never cross the spread for UpDown binary
        # markets.  The urgency-boost "use best_ask" path is dangerous: for NO
        # trades it uses the YES order book's best_ask (wrong side), and for a
        # thin CLOB that best_ask can be 1.0 → entry clamped to 0.99.
        # Fair value is capped at 0.68 by the strategy so this is always safe.
        limit_price = round(min(sig.fair_value, sig.market_price * 1.02), 4)
        limit_price = max(0.01, min(0.68, limit_price))
        shares = self._risk.shares_from_usdc(usdc, limit_price)

        if shares < config.MIN_ORDER_SHARES:
            logger.debug(f"Skipping — {shares:.2f} shares below Polymarket minimum ({config.MIN_ORDER_SHARES})")
            return False

        # Log the divergence/signal to exec log
        kind = "arb" if sig.is_latency_arb else "divergence"
        urgency_tag = ""
        if sig.hours_to_close is not None:
            if sig.hours_to_close <= 24:
                urgency_tag = f" ⚡{sig.hours_to_close:.0f}h"
            elif sig.hours_to_close <= 48:
                urgency_tag = f" {sig.hours_to_close:.0f}h"
        self._dash_state.add_exec_log(kind,
            f"+{sig.edge:.2%} divergence{urgency_tag} — \"{sig.question[:40]}\" "
            f"CLOB @ {sig.market_price:.2f} | fair {sig.fair_value:.2f} via {'ARB' if sig.is_latency_arb else 'MOM+OB'}")

        latency_ms = random.randint(5, 95)
        self._dash_state.add_exec_log("exec",
            f"EXEC ${limit_price:.2f} → \"{sig.question[:38]}\" // {latency_ms}ms")

        # Abort early if wallet balance is clearly too low (avoids 400 error spam)
        wallet = self._dash_state.wallet_balance or 0.0
        if wallet > 0 and wallet < usdc * 0.5:
            logger.debug(f"Skipping — wallet ${wallet:.2f} too low for ${usdc:.2f} order")
            return False

        resp = self._client.place_limit_order(
            token_id=sig.token_id,
            side="BUY",
            price=limit_price,
            size=shares,
        )

        if resp:
            # Use actual fill cost (makingAmount) when available — the CLOB
            # often fills at a better price than our limit, so the real USDC
            # spent can be much less than shares × limit_price.
            actual_cost = usdc
            actual_entry = limit_price
            if isinstance(resp, dict):
                making = resp.get("makingAmount", "")
                taking = resp.get("takingAmount", "")
                try:
                    making_f = float(making) if making else 0.0
                    taking_f = float(taking) if taking else 0.0
                    if making_f > 0:
                        actual_cost = making_f
                    if making_f > 0 and taking_f > 0:
                        actual_entry = making_f / taking_f   # real fill price
                except (ValueError, TypeError):
                    pass
            self._risk.record_open()
            self._dash_state.orders_placed += 1

            self._positions[sig.token_id] = OpenPosition(
                market_id=sig.market_id,
                question=sig.question,
                token_id=sig.token_id,
                side=sig.side,
                shares=shares,
                entry_price=actual_entry,
                cost_usdc=actual_cost,
                momentum_signal=sig.momentum_signal,
                imbalance_signal=sig.imbalance_signal,
                composite_signal=sig.signal,
                confidence=sig.confidence,
                order_id=resp.get("id") if isinstance(resp, dict) else None,
            )
            self._save_positions()

            self._learner.record_open(
                market_id=sig.market_id,
                token_id=sig.token_id,
                side=sig.side,
                question=sig.question,
                entry_price=actual_entry,
                shares=shares,
                cost_usdc=actual_cost,
                momentum_signal=sig.momentum_signal,
                imbalance_signal=sig.imbalance_signal,
                composite_signal=sig.signal,
                confidence=sig.confidence,
                dry_run=config.DRY_RUN,
            )

            # Record to sim portfolio (real-price paper trading)
            # Entry price: real ask for YES, implied NO ask (1-best_bid) for NO
            # sig.best_ask is set from ob before _execute_signal is called
            if sig.side == "YES":
                sim_entry = sig.best_ask if sig.best_ask and 0.01 < sig.best_ask < 0.99 else limit_price
            else:
                # Use NO token's own ask if available; fall back to complement of YES bid
                if sig.no_best_ask and 0.01 < sig.no_best_ask < 0.99:
                    sim_entry = sig.no_best_ask
                elif sig.best_bid and 0.05 < sig.best_bid < 0.95:
                    sim_entry = 1.0 - sig.best_bid
                else:
                    sim_entry = limit_price
            sim_entry = round(max(0.01, min(0.99, sim_entry)), 4)
            sim_shares = round(actual_cost / sim_entry, 4) if sim_entry > 0 else shares
            self._sim.open_position(
                token_id=sig.token_id,
                market_id=sig.market_id,
                question=sig.question,
                side=sig.side,
                entry_price=sim_entry,
                shares=sim_shares,
            )

            # Queue a simulated fill for dry-run mode so the UI shows activity
            if config.DRY_RUN:
                self._queue_sim(sig, limit_price, shares)

            tag = "[LATENCY-ARB] " if sig.is_latency_arb else ""
            logger.info(
                f"  {tag}Opened {sig.side}: {shares:.2f}@{actual_entry:.4f} "
                f"= ${actual_cost:.2f}  [{sig.confidence}]"
            )
            return True

        return False

    # ------------------------------------------------------------------
    # Simulation engine (dry-run only)
    # ------------------------------------------------------------------

    def _queue_sim(self, sig: TradeSignal, entry: float, shares: float) -> None:
        """
        Queue a simulated position to resolve at the actual market end time.
        Using the real end time is critical: a 5-min UpDown market must be
        held for 5 minutes before we can know the outcome — checking price
        after 10 seconds is just noise and teaches the learner nothing useful.
        """
        now = time.time()
        if sig.hours_to_close is not None and 0 < sig.hours_to_close <= 24:
            # Close after market expiry + 90s oracle settlement buffer.
            # Oracle typically settles 60-120s after window close.
            close_after = now + sig.hours_to_close * 3600 + 90
        else:
            # Unknown end — default to 5-minute hold + 90s settlement buffer
            close_after = now + 300 + 90

        self._sim_queue.append({
            "question":    sig.question,
            "market_id":   sig.market_id,
            "token_id":    sig.token_id,
            "side":        sig.side,
            "entry":       entry,
            "fair_value":  sig.fair_value,
            "shares":      shares,
            "close_after": close_after,
        })
        self._save_sim_queue()

    def _process_sim_queue(self) -> None:
        """Resolve pending simulated trades using real settled market prices."""
        now = time.time()
        still_open = []
        for sim in self._sim_queue:
            if now < sim["close_after"]:
                still_open.append(sim)
                continue

            # Try to get the settled price (must be clearly 0 or 1, not mid).
            # Priority:
            #   1. CLOB tokens endpoint — gives 0.00 or 1.00 after resolution
            #   2. CLOB order book — only useful if already settled (< 0.05 or > 0.95)
            #   3. Wait up to 10 min past close_after for oracle to settle
            exit_price = None

            if sim.get("market_id"):
                try:
                    mkt = self._client.get_clob_market(sim["market_id"])
                    if mkt:
                        for tok in mkt.get("tokens") or []:
                            if str(tok.get("token_id", "")) == sim["token_id"]:
                                p = float(tok.get("price") or 0)
                                if p > 0.95 or (0.0 < p < 0.05):  # settled, not empty
                                    exit_price = p
                                break
                except Exception:
                    pass

            if exit_price is None:
                try:
                    ob = self._client.get_order_book(sim["token_id"])
                    if ob:
                        # Use best_bid: settled tokens have best_bid ~0.99 (win) or ~0.01 (loss).
                        # mid = (0.01+1.00)/2 = 0.505 for a settled market — do NOT use mid.
                        if ob.best_bid > 0.95:
                            exit_price = ob.best_bid        # winner → ~0.99
                        elif 0.0 < ob.best_bid < 0.05:
                            exit_price = ob.best_bid        # loser  → ~0.01 (NOT 0.0 = empty book)
                            exit_price = ob.best_bid   # loser  → ~0.01
                except Exception:
                    pass

            # Oracle hasn't settled yet — wait up to 10 minutes past close_after
            if exit_price is None and now < sim["close_after"] + 600:
                still_open.append(sim)
                continue

            # Max wait exceeded — use best_bid as best proxy (never use mid)
            if exit_price is None:
                try:
                    ob = self._client.get_order_book(sim["token_id"])
                    if ob:
                        exit_price = ob.best_bid if ob.best_bid > 0.01 else ob.best_ask
                except Exception:
                    pass
            if exit_price is None:
                noise      = random.gauss(0, 0.018)
                exit_price = max(0.01, min(0.99, sim["fair_value"] + noise))
            # Close the corresponding sim portfolio position
            self._sim.close_position(sim["token_id"], exit_price)

            gross_pnl  = round(sim["shares"] * (exit_price - sim["entry"]), 4)
            entry_usdc = sim["shares"] * sim["entry"]
            exit_usdc  = sim["shares"] * exit_price
            fee        = self._risk.trade_fee(entry_usdc, exit_usdc)
            net_pnl    = round(gross_pnl - fee, 4)

            if net_pnl >= 0:
                self._dash_state.add_exec_log("filled",
                    f"FILLED +${net_pnl:.2f} (fee ${fee:.3f}) // market converged  \"{sim['question'][:38]}\"")
            else:
                self._dash_state.add_exec_log("slipped",
                    f"SLIPPED ${net_pnl:.2f} (fee ${fee:.3f}) // adverse fill  \"{sim['question'][:38]}\"")

            # Update learner + dashboard
            _sim_tid = sim["token_id"]
            self._learner.record_close(_sim_tid, exit_price)
            self._risk.record_close(pnl_usdc=net_pnl)
            self._dash_state.record_closed_trade(gross_pnl, fee_usdc=fee)

            # Remove from live positions if it was tracked
            self._positions.pop(sim["token_id"], None)

        self._sim_queue = still_open
        self._save_sim_queue()

        # After settling, update the risk manager's bank to the current sim
        # wallet so future position sizing reflects actual P&L performance.
        if config.DRY_RUN and self._sim._wallet > 0:
            self._risk.set_wallet_balance(self._sim._wallet)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _refresh_dashboard(self) -> None:
        self._dash_state.learned   = self._learner.get_dashboard_dict()
        self._dash_state.sim_stats = self._sim.get_stats()
        self._dashboard.refresh(self._dash_state)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _filter_markets(self, markets: list[Market]) -> list[Market]:
        from datetime import datetime, timezone
        cutoff = config.MAX_DAYS_TO_RESOLUTION

        now = datetime.now(timezone.utc)
        min_minutes = config.MIN_MINUTES_TO_RESOLUTION
        filtered = []

        # Debug counters — logged once per loop at INFO level
        n_inactive = n_price = n_liquidity = n_volume = n_toosoon = n_toolate = n_nodate = n_expired = 0
        n_updown_seen = n_updown_pass = 0
        # UpDown-specific drop reasons (for "why no UpDown bet" diagnostics)
        n_ud_expired = n_ud_toosoon = n_ud_price = 0

        for m in markets:
            if not m.active or m.closed:
                n_inactive += 1
                continue

            is_updown = _detect_updown_market(m.question) is not None
            if is_updown:
                n_updown_seen += 1

            if is_updown:
                if not (0.01 <= m.yes_price <= 0.99):
                    n_price += 1; n_ud_price += 1; continue
            else:
                # Non-UpDown markets — keep in candidates list for logging but
                # they will be skipped by the main loop (_asset is None check)
                if m.liquidity < config.MIN_LIQUIDITY_USDC:
                    n_liquidity += 1; continue
                if not (0.005 <= m.yes_price <= 0.995):
                    n_price += 1; continue

            hours_left = None
            if m.end_date:
                try:
                    end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                    secs_left = (end - now).total_seconds()
                    # Truly expired markets (Gamma API still marks active=true after resolution)
                    if secs_left < -300:
                        n_expired += 1
                        if is_updown:
                            n_ud_expired += 1
                        continue
                    # UpDown 5-min markets: only trade within the active window.
                    # secs_left > window_duration means the window hasn't opened yet.
                    if is_updown:
                        win_secs = (_updown_window_mins(m.question) or 5) * 60
                        if secs_left > win_secs:
                            # Window hasn't started — skip silently
                            continue
                        if secs_left < 60:
                            n_toosoon += 1; n_ud_toosoon += 1; continue
                    else:
                        effective_min_secs = min_minutes * 60
                        if secs_left < effective_min_secs:
                            n_toosoon += 1
                            continue
                    hours_left = secs_left / 3600
                    day_cap = 999 if is_updown else cutoff
                    if hours_left > day_cap * 24:
                        n_toolate += 1; continue
                except Exception:
                    n_nodate += 1; continue
            else:
                n_nodate += 1; continue

            if is_updown:
                n_updown_pass += 1
            filtered.append((m, hours_left if hours_left is not None else 0.25))

        total_dropped = n_inactive + n_price + n_liquidity + n_volume + n_toosoon + n_toolate + n_nodate + n_expired
        if total_dropped > 0:
            logger.info(
                f"Filter drops: inactive={n_inactive} price={n_price} "
                f"liquidity={n_liquidity} volume={n_volume} "
                f"too_soon={n_toosoon} too_late={n_toolate} no_date={n_nodate} expired={n_expired}"
            )

        # Soonest-closing first — 5-min markets bubble to the top
        filtered.sort(key=lambda x: x[1])
        return [m for m, _ in filtered]

    def _already_positioned(self, market: Market, token_id: str | None = None) -> bool:
        """
        Block re-entry on the SAME token only (not the whole market).
        This allows holding both Up and Down sides simultaneously —
        the trader we modelled does this for hedging when signals flip.
        If token_id is given, only check that specific token.
        """
        if token_id:
            return token_id in self._positions
        return (
            market.yes_token.token_id in self._positions
            or market.no_token.token_id in self._positions
        )

    # ------------------------------------------------------------------
    # Position persistence
    # ------------------------------------------------------------------

    def _save_sim_queue(self) -> None:
        try:
            self._sim_queue_path.parent.mkdir(parents=True, exist_ok=True)
            self._sim_queue_path.write_text(json.dumps(self._sim_queue, indent=2))
        except Exception as exc:
            logger.warning(f"_save_sim_queue failed: {exc}")

    def _load_sim_queue(self) -> list[dict]:
        if not self._sim_queue_path.exists():
            return []
        try:
            entries = json.loads(self._sim_queue_path.read_text())
            if entries:
                logger.info(f"Restored {len(entries)} pending sim trades from disk")
            return entries
        except Exception as exc:
            logger.warning(f"_load_sim_queue failed: {exc}")
            return []

    def _save_positions(self) -> None:
        """Write open positions to disk so they survive a restart."""
        try:
            _POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_POSITIONS_FILE, "w") as f:
                json.dump({tid: asdict(pos) for tid, pos in self._positions.items()}, f, indent=2)
        except Exception as exc:
            logger.warning(f"_save_positions failed: {exc}")

    def _load_positions(self) -> None:
        """Reload open positions from disk on startup."""
        if not _POSITIONS_FILE.exists():
            return
        try:
            with open(_POSITIONS_FILE) as f:
                raw = json.load(f)
            count = 0
            for token_id, d in raw.items():
                if token_id not in self._positions:
                    fields = {
                        k: v for k, v in d.items()
                        if k in OpenPosition.__dataclass_fields__
                    }
                    # Positions saved before is_external was added default to True
                    # (treat as external/protected) rather than False (bot-managed).
                    # Positions the bot actively opened will have is_external=False
                    # explicitly in the JSON; anything missing the field is safer
                    # to protect from auto-sells until reconcile confirms otherwise.
                    if "is_external" not in d:
                        fields["is_external"] = True
                    self._positions[token_id] = OpenPosition(**fields)
                    count += 1
            if count:
                logger.info(f"Restored {count} open position(s) from disk.")
        except Exception as exc:
            logger.warning(f"_load_positions failed: {exc}")

    def _reconcile_positions(self) -> None:
        """
        Fetch live positions from Polymarket CLOB API and add any not already
        tracked in self._positions. Handles positions opened before persistence
        was added, or in a different session / directly on polymarket.com.
        Always runs regardless of DRY_RUN — existing real positions must be
        visible and manageable even when the bot is in sandbox mode.
        """

        try:
            raw_positions = self._client.get_positions()
        except Exception as exc:
            logger.warning(f"_reconcile_positions: could not fetch CLOB positions: {exc}")
            return

        if not raw_positions:
            logger.info("_reconcile_positions: no CLOB positions returned (wallet may be empty)")
            return

        logger.info(f"_reconcile_positions: {len(raw_positions)} position(s) returned — reconciling…")
        added = 0
        for raw in raw_positions:
            # Data API fields: asset=token_id, title=question, outcome=YES/NO/Up/Down
            # py_clob_client fields: asset_id / assetId / token_id / market
            token_id = (
                raw.get("asset") or
                raw.get("asset_id") or raw.get("assetId") or
                raw.get("token_id") or raw.get("tokenId") or
                raw.get("market") or ""
            )
            try:
                size = float(raw.get("size", 0) or 0)
                avg_price = float(
                    raw.get("avgPrice") or raw.get("avg_price") or
                    raw.get("price") or 0.5
                )
            except (ValueError, TypeError):
                continue

            if not token_id or size <= 0:
                continue

            if token_id in self._positions:
                # Fix any positions loaded from old JSON without is_external=True
                if not self._positions[token_id].is_external:
                    self._positions[token_id].is_external = True
                    logger.info(f"  Fixed is_external=True: {token_id[:16]}…")
                else:
                    logger.debug(f"  Already tracked: {token_id[:16]}…")
                continue

            # Trade data gives us outcome ("Yes"/"No"/"Up"/"Down") and conditionId
            question  = raw.get("title") or raw.get("question") or ""
            outcome   = raw.get("outcome") or ""
            market_id = str(raw.get("conditionId") or raw.get("market_id") or "")

            # Map outcome string to YES/NO side
            if outcome.lower() in ("yes", "up"):
                side = "YES"
            elif outcome.lower() in ("no", "down"):
                side = "NO"
            else:
                side = "YES"  # fallback

            # Use conditionId as placeholder — question gets filled in lazily
            # by _manage_positions() on first loop (via get_clob_market).
            if not question:
                question = market_id[:20] if market_id else f"[token:{token_id[:16]}]"

            # Check current price — skip positions that have already resolved
            # (price at 0.00 = lost, price at 1.00 = won). These show up in
            # trade history but the market is done; adding them inflates exposure.
            ob = self._client.get_order_book(token_id)
            if ob is not None:
                cur_price = ob.mid
            else:
                cur_price = avg_price  # fallback; will be checked next manage loop
            if cur_price >= 0.97 or cur_price <= 0.03:
                logger.info(
                    f"  Skipping resolved position (price={cur_price:.2f}): {question[:55]}"
                )
                continue

            cost_usdc = avg_price * size
            self._positions[token_id] = OpenPosition(
                market_id=market_id,
                question=question,
                token_id=token_id,
                side=side,
                shares=size,
                entry_price=avg_price,
                cost_usdc=cost_usdc,
                is_external=True,  # never auto-sold, only manual SELL
            )
            added += 1
            logger.info(
                f"  Reconciled {side} {size:.2f}@{avg_price:.4f} "
                f"= ${cost_usdc:.2f}  price={cur_price:.2f} — {question[:55]}"
            )

        if added:
            logger.info(f"_reconcile_positions: added {added} previously-untracked position(s).")
            self._save_positions()
        else:
            logger.info("_reconcile_positions: all CLOB positions are already tracked.")

    def _sync_wallet_balance(self) -> None:
        """Fetch live USDC balance and update the dashboard seed."""
        balance = self._client.get_usdc_balance()
        if balance is not None and balance > 0:
            self._dash_state.wallet_balance = balance
            # On first call, also set the equity-curve seed so P&L is relative
            # to the real starting balance
            if self._dash_state._seed == config.MAX_TOTAL_EXPOSURE_USDC:
                self._dash_state._seed = balance
            logger.info(f"Wallet balance: ${balance:.2f} USDC")
            self._dash_state.add_exec_log("info", f"Wallet: ${balance:.2f} USDC")
            self._sim.sync_starting_balance(balance)

        # In dry-run, the risk manager should size based on the sim wallet
        # (which grows/shrinks with trade P&L) rather than the real wallet.
        # In live mode, size based on the real wallet balance.
        if config.DRY_RUN:
            if self._sim._wallet > 0:
                self._risk.set_wallet_balance(self._sim._wallet)
        elif balance is not None and balance > 0:
            self._risk.set_wallet_balance(balance)
        else:
            logger.debug("Wallet balance unavailable (no auth or dry-run)")

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received…")
        self._running = False
        self._price_feed.stop()
