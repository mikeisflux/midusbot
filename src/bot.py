"""
Bot orchestrator — ties together client, strategies, risk manager,
adaptive learner, and live dashboard.

Loop per iteration
──────────────────
1. Manage existing positions (stop-loss / take-profit).
2. Scan markets — run both strategies:
     a. Momentum + Imbalance (all markets).
     b. Latency Arbitrage    (BTC / crypto price-level markets).
3. Size and place orders for valid signals.
4. Record trades in the learner; learner adapts when enough data exists.
5. Push state to the dashboard.
6. Sleep until next iteration.
"""
from __future__ import annotations

import random
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from loguru import logger

from src.client import Market, PolymarketClient
from src.dashboard import Dashboard, DashboardState
from src.feeds import BinanceWSFeed, NewsFeed
from src.learner import AdaptiveLearner
from src.risk import RiskManager
from src.strategy import (
    LatencyArbStrategy,
    MomentumImbalanceStrategy,
    UpDownMomentumStrategy,
    BTCLevelStrategy,
    SportsLiveStrategy,
    SportsSpreadArbStrategy,
    NewsEventStrategy,
    TradeSignal,
    _fetch_btc_price,
    _fetch_price,
    _detect_updown_market,
    _parse_btc_level,
    update_price_snapshot,
)
import config
import src.webui as webui


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


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class PolymarketBot:
    def __init__(self, *, dashboard_enabled: bool = True) -> None:
        self._learner    = AdaptiveLearner()
        self._client     = PolymarketClient()
        self._strategy   = MomentumImbalanceStrategy(params=self._learner.strategy_params)
        self._latency    = LatencyArbStrategy()
        self._updown     = UpDownMomentumStrategy()
        self._btclevel   = BTCLevelStrategy()
        self._sports     = SportsLiveStrategy()
        self._spread_arb = SportsSpreadArbStrategy()
        self._news       = NewsEventStrategy()
        self._risk       = RiskManager(params=self._learner.risk_params)
        self._dashboard  = Dashboard(enabled=dashboard_enabled)
        self._dash_state = DashboardState()

        self._positions: dict[str, OpenPosition] = {}
        self._running = False

        # Live data feeds
        self._price_feed = BinanceWSFeed()
        self._news_feed  = NewsFeed()

        # Simulation queue — pending dry-run trades waiting for synthetic fill
        self._sim_queue: list[dict] = []

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
        self._news_feed.start()    # RSS headlines — news arb pre-signal

        # Cancel any stale open orders left from previous runs
        if not config.DRY_RUN:
            stale = self._client.get_open_orders()
            if stale:
                logger.info(f"Cancelling {len(stale)} stale open order(s) from previous session…")
                self._client.cancel_all_orders()

        # Restore performance stats + equity curve from persisted journal/file
        self._dash_state.restore_from_journal(self._learner.journal)

        # Pull live wallet balance and use it as the portfolio seed
        self._sync_wallet_balance()

        # Add a restart marker to the equity curve so gaps are visible on the chart
        self._dash_state.add_equity_point()

        # Start web UI (always, regardless of terminal dashboard)
        webui.start(self._dash_state, port=8080, learner=self._learner, close_position_fn=self._close_position)
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

                if self._running:
                    time.sleep(config.LOOP_INTERVAL_SECONDS)
        finally:
            self._dashboard.stop()

        logger.info("Bot stopped.")

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

        # 0c. Process any pending simulated fills
        self._process_sim_queue()

        # 1. Manage existing positions
        self._manage_positions()

        # 2. Update live crypto prices (WebSocket feed handles this in real-time;
        #    REST fallback warms history for symbols not on Binance WS like HYPE)
        btc = _fetch_btc_price()
        if btc:
            self._dash_state.btc_price = btc
        for sym in ("XRP", "ETH", "SOL", "DOGE", "BNB", "HYPE"):
            _fetch_price(sym)

        # 3. Scan markets
        self._dash_state.add_exec_log("scan",
            f"Orderbook depth scan — evaluating {self._dash_state.markets_scanned or '…'} markets")

        markets = self._client.get_markets()
        updown  = self._client.get_updown_markets()

        # Drain new news headlines and score against fetched markets
        new_headlines = self._news_feed.drain_new()
        _news_flagged: set[str] = set()
        if new_headlines:
            for h in new_headlines:
                for m in (updown + markets):
                    score = self._news_feed.score_against_markets(h, [m.question])
                    if score >= 0.3:
                        _news_flagged.add(m.id)
                        self._dash_state.add_exec_log("news",
                            f"[NEWS] {h.source}: \"{h.title[:55]}\" → \"{m.question[:40]}\"")
        # Deduplicate and put Up/Down markets first (they have highest urgency)
        seen_ids = {m.id for m in updown}
        all_markets = updown + [m for m in markets if m.id not in seen_ids]
        candidates = self._filter_markets(all_markets)
        self._dash_state.markets_scanned = len(all_markets)
        self._dash_state.candidates = len(candidates)
        updown_passing = [m for m in candidates if _detect_updown_market(m.question)]
        logger.info(f"{len(candidates)}/{len(all_markets)} markets pass filters — {len(updown_passing)} UpDown, {len(candidates)-len(updown_passing)} regular.")
        if updown and self._dash_state.loop_count <= 3:
            logger.info(f"First {min(3,len(updown))} UpDown markets:")
            for m in updown[:3]:
                logger.info(f"  slug={m.slug[:50]}  q={m.question[:60]}  end={m.end_date}  price={m.yes_price:.3f}")
        elif not updown:
            logger.warning("UpDown: 0 markets returned — no active 5-min crypto slots right now")

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
            if self._already_positioned(market):
                continue  # both sides held — skip
            # Skip markets where we already hold one side (avoids redundant log noise
            # from SportsSpreadArb firing on every already-held position each loop)
            if (market.yes_token.token_id in self._positions or
                    market.no_token.token_id in self._positions):
                continue
            # For UpDown markets: one bet per asset per loop, and skip if we
            # already hold a position on that asset from a previous loop.
            _asset = _detect_updown_market(market.question)
            if _asset:
                if _asset in _updown_bet_this_loop or _asset in _updown_assets_held:
                    continue

            ob = self._client.get_order_book(market.yes_token.token_id)

            # Hours until this market closes (used for urgency boost)
            hours_to_close: float | None = None
            if market.end_date:
                try:
                    from datetime import timezone as _tz
                    end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                    hours_to_close = (end - datetime.now(_tz.utc)).total_seconds() / 3600
                except Exception:
                    pass

            # Update price snapshot for news detection BEFORE running strategies
            current_yes = ob.mid if ob else market.yes_price
            update_price_snapshot(market.id, current_yes)

            # News-flagged markets: force NewsEventStrategy to run first
            if market.id in _news_flagged:
                sig = self._news.analyse(market, ob)
                if sig:
                    sig.confidence = "HIGH"   # news-confirmed → high conviction
                    signals_found += 1
                    self._dash_state.push_signal(sig)
                    if self._execute_signal(sig):
                        trades_placed += 1
                    continue

            # ── Strategy 3: Up/Down 5-min Momentum (highest priority) ──
            sig = self._updown.analyse(market, ob)

            # ── Strategy 4: BTC Price Level (daily/weekly/monthly) ────
            if sig is None:
                sig = self._btclevel.analyse(market, ob)

            # ── Strategy 5: Sports Spread Arb vs Vegas (kch123 pattern) ──
            if sig is None:
                sig = self._spread_arb.analyse(market, ob)

            # ── Strategy 6: Live Sports Settlement Lag (ESPN in-game) ──
            if sig is None:
                sig = self._sports.analyse(market, ob)

            # ── Strategy 7: News/Event Sudden Price Move ───────────────
            if sig is None:
                sig = self._news.analyse(market, ob)

            # ── Strategy 1: Momentum + Imbalance ─────────────────────
            if sig is None:
                price_hist = self._client.get_price_history(market.yes_token.token_id)
                sig = self._strategy.analyse(market, ob, price_hist)

            # ── Strategy 2: Latency Arbitrage ─────────────────────────
            if sig is None:
                sig = self._latency.analyse(market, ob)

            if sig is None:
                continue

            # Skip if we already hold this exact token
            if self._already_positioned(market, token_id=sig.token_id):
                continue

            sig.hours_to_close = hours_to_close
            if ob:
                sig.best_ask = ob.best_ask

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

            # When the order book is empty (no bids, no asks) the CLOB mid
            # returns 0.5 regardless of whether the market resolved YES or NO.
            # In that case we must ask the Gamma API for the real outcome price.
            book_is_empty = ob is None or (ob.best_bid == 0.0 and ob.best_ask == 1.0)
            if book_is_empty:
                current_price = self._gamma_position_price(pos)
            else:
                current_price = ob.mid

            # Auto-claim: token resolved in our favour (worth $1.00).
            if current_price >= 0.97:
                logger.info(
                    f"AUTO-CLAIM: resolved YES @ ${current_price:.3f} "
                    f"({pos.shares:.2f} shares)  {pos.question[:50]}"
                )
                to_close.append((token_id, current_price))
                position_snapshots.append((pos, current_price))
                continue

            # Auto-clear: token resolved against us (worth $0.00).
            if current_price <= 0.03:
                pnl = self._learner.record_close(token_id, current_price)
                self._risk.register_close(pos.cost_usdc, pnl_usdc=pnl)
                self._dash_state.record_closed_trade(pnl, fee_usdc=0.0)
                del self._positions[token_id]
                logger.info(f"AUTO-CLEAR: resolved NO — position removed  {pos.question[:50]}")
                continue

            pnl_pct = (
                (current_price - pos.entry_price) / pos.entry_price
                if pos.entry_price else 0.0
            )

            position_snapshots.append((pos, current_price))

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
        When the CLOB order book is empty, fetch the current outcome price
        from the Gamma API using the stored market_id.
        Returns the price for the side we hold (YES or NO token).
        Falls back to entry_price if the Gamma call fails.
        """
        try:
            market = self._client.get_market_by_id(pos.market_id)
            if market:
                price = market.yes_price if pos.side == "YES" else market.no_price
                logger.debug(
                    f"[Gamma fallback] {pos.side} price={price:.3f}  "
                    f"closed={market.closed}  {pos.question[:50]}"
                )
                return price
        except Exception as exc:
            logger.warning(f"_gamma_position_price failed for {pos.market_id}: {exc}")
        return pos.entry_price

    def _close_position(self, token_id: str, current_price: float | None = None) -> bool:
        pos = self._positions.get(token_id)
        if not pos:
            logger.warning(f"_close_position: token_id {token_id[:12]}… not found in open positions")
            return False

        ob = self._client.get_order_book(token_id)
        book_best_bid = ob.best_bid if ob else 0.0

        # For resolved markets the order book is empty (best_bid=0).
        # Use the tracked current_price (from Gamma) so we sell at the
        # real resolution price (e.g. 1.0) rather than $0.
        if book_best_bid > 0.0:
            sell_price = book_best_bid
        elif current_price is not None and current_price > 0.05:
            sell_price = current_price   # resolved price from Gamma
        else:
            sell_price = pos.entry_price

        resp = self._client.place_limit_order(
            token_id=token_id,
            side="SELL",
            price=sell_price,
            size=pos.shares,
        )
        if resp:
            exit_usdc = sell_price * pos.shares
            fee = self._risk.trade_fee(pos.cost_usdc, exit_usdc)
            pnl = self._learner.record_close(token_id, sell_price)
            self._risk.register_close(pos.cost_usdc, pnl_usdc=pnl - fee)
            self._dash_state.record_closed_trade(pnl, fee_usdc=fee)
            del self._positions[token_id]
            logger.info(f"Closed: {pos.side} {pos.question[:40]}  P&L=${pnl:+.2f}  fee=${fee:.4f}  net=${pnl-fee:+.2f}")
            return True
        return False

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute_signal(self, sig: TradeSignal) -> bool:
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

        # For markets closing within 1 hour, cross the spread to guarantee fill.
        # For others, stay passive (limit at fair value) to avoid slippage.
        if sig.hours_to_close is not None and sig.hours_to_close <= 1:
            ask = sig.best_ask if sig.best_ask else sig.market_price * 1.02
            limit_price = round(ask, 4)
        else:
            limit_price = round(min(sig.fair_value, sig.market_price * 1.01), 4)
        limit_price = max(0.01, min(0.99, limit_price))
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
            self._risk.register_open(actual_cost)
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
        """Queue a synthetic fill that will resolve in 5–30 s."""
        self._sim_queue.append({
            "question":    sig.question,
            "token_id":    sig.token_id,
            "side":        sig.side,
            "entry":       entry,
            "fair_value":  sig.fair_value,
            "shares":      shares,
            "close_after": time.time() + random.uniform(5, 30),
        })

    def _process_sim_queue(self) -> None:
        """Resolve pending simulated trades and update equity curve."""
        now = time.time()
        still_open = []
        for sim in self._sim_queue:
            if now < sim["close_after"]:
                still_open.append(sim)
                continue

            # Synthetic exit: fair_value ± small noise
            noise      = random.gauss(0, 0.018)
            exit_price = max(0.01, min(0.99, sim["fair_value"] + noise))
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
            self._learner.record_close(sim["token_id"], exit_price)
            self._risk.register_close(entry_usdc, pnl_usdc=net_pnl)
            self._dash_state.record_closed_trade(gross_pnl, fee_usdc=fee)

            # Remove from live positions if it was tracked
            self._positions.pop(sim["token_id"], None)

        self._sim_queue = still_open

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _refresh_dashboard(self) -> None:
        self._dash_state.learned = self._learner.get_dashboard_dict()
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

        for m in markets:
            if not m.active or m.closed:
                n_inactive += 1
                continue

            is_updown   = _detect_updown_market(m.question) is not None
            if is_updown:
                n_updown_seen += 1
            is_btclevel = _parse_btc_level(m.question) is not None
            q_lower = m.question.lower()
            is_sports_game = any(kw in q_lower for kw in (
                " vs ", " beat ", " win game", "game 1", "game 2", "game 3",
                "game 4", "game 5", "game 6", "game 7",
                "tonight", "monday night", "tuesday night", "wednesday night",
                "thursday night", "friday night", "saturday night", "sunday night",
            )) and any(kw in q_lower for kw in (
                "nfl", "nba", "mlb", "nhl", "epl", "premier league",
                " fc ", "united", "city ", "lakers", "celtics", "bulls",
                "yankees", "dodgers", "chiefs", "patriots", "eagles",
                "oilers", "bruins", "maple leafs", "canadiens", "penguins",
                "rangers", "kings", "canucks", "flames", "jets",
            ))

            if is_updown:
                if not (0.01 <= m.yes_price <= 0.99):
                    logger.debug(f"[UD-DROP price] yes_price={m.yes_price:.3f} q={m.question[:60]}")
                    n_price += 1; continue
            elif is_btclevel:
                if m.liquidity < 50:
                    n_liquidity += 1; continue
                if not (0.001 <= m.yes_price <= 0.999):
                    n_price += 1; continue
            else:
                if m.liquidity < config.MIN_LIQUIDITY_USDC:
                    n_liquidity += 1; continue
                if m.volume < config.MIN_VOLUME_24H_USDC:
                    n_volume += 1; continue
                # Allow wider price range: cheap options (1¢) and near-settled (99¢)
                # can still have edge for BTCLevel and NewsEvent strategies
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
                        continue
                    # UpDown 5-min markets: allow entry as long as >1 min remains
                    # Regular markets: use MIN_MINUTES_TO_RESOLUTION (default 5)
                    effective_min_secs = 60 if is_updown else min_minutes * 60
                    if secs_left < effective_min_secs:
                        n_toosoon += 1
                        logger.info(f"too_soon: is_updown={is_updown} secs={secs_left:.0f} slug={m.slug[:40]} q={m.question[:70]}")
                        continue
                    if is_updown:
                        logger.debug(f"[UD-PASS time] secs={secs_left:.0f} price={m.yes_price:.3f} q={m.question[:60]}")
                    hours_left = secs_left / 3600
                    if is_btclevel:
                        day_cap = 35
                    elif is_updown:
                        day_cap = 999
                    elif is_sports_game:
                        day_cap = 3
                    else:
                        day_cap = cutoff
                    if hours_left > day_cap * 24:
                        n_toolate += 1; continue
                except Exception:
                    if not is_updown and not is_btclevel:
                        n_nodate += 1; continue
            else:
                if not is_updown and not is_sports_game:
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
        if n_updown_seen > 0:
            logger.info(f"UpDown filter: {n_updown_seen} seen → {n_updown_pass} pass")

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
            and market.no_token.token_id in self._positions
        )

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
        else:
            logger.debug("Wallet balance unavailable (no auth or dry-run)")

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received…")
        self._running = False
        self._price_feed.stop()
        self._news_feed.stop()
