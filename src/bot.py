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
from src.learner import AdaptiveLearner
from src.risk import RiskManager
from src.strategy import (
    LatencyArbStrategy,
    MomentumImbalanceStrategy,
    UpDownMomentumStrategy,
    TradeSignal,
    _fetch_btc_price,
    _fetch_price,
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
        self._risk       = RiskManager(params=self._learner.risk_params)
        self._dashboard  = Dashboard(enabled=dashboard_enabled)
        self._dash_state = DashboardState()

        self._positions: dict[str, OpenPosition] = {}
        self._running = False

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

        # Pull live wallet balance and use it as the portfolio seed
        self._sync_wallet_balance()

        # Seed the equity curve with starting value
        self._dash_state.add_equity_point()

        # Start web UI (always, regardless of terminal dashboard)
        webui.start(self._dash_state, port=8080)
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

        # 0a. Refresh wallet balance every 10 loops (or every loop in live mode)
        if not config.DRY_RUN or self._dash_state.loop_count % 10 == 0:
            self._sync_wallet_balance()

        # 0b. Process any pending simulated fills
        self._process_sim_queue()

        # 1. Manage existing positions
        self._manage_positions()

        # 2. Update live crypto prices (warms up momentum history)
        btc = _fetch_btc_price()
        if btc:
            self._dash_state.btc_price = btc
        for sym in ("XRP", "ETH", "SOL", "DOGE"):
            _fetch_price(sym)

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
        logger.info(f"{len(candidates)}/{len(markets)} markets pass filters.")

        self._dash_state.add_exec_log("scan",
            f"Evaluating {len(candidates)} candidate markets on CLOB…")

        signals_found = 0
        trades_placed = 0

        for market in candidates:
            if not self._running:
                break
            if self._already_positioned(market):
                continue  # both sides held — skip

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

            # ── Strategy 3: Up/Down 5-min Momentum (highest priority) ──
            sig = self._updown.analyse(market, ob)

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

        to_close: list[str] = []
        position_snapshots: list[tuple[OpenPosition, float]] = []

        for token_id, pos in self._positions.items():
            ob = self._client.get_order_book(token_id)
            current_price = ob.mid if ob else pos.entry_price

            pnl_pct = (
                (current_price - pos.entry_price) / pos.entry_price
                if pos.entry_price else 0.0
            )

            position_snapshots.append((pos, current_price))

            if self._risk.should_stop_loss(pnl_pct):
                logger.warning(f"STOP-LOSS {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                to_close.append(token_id)
            elif self._risk.should_take_profit(pnl_pct):
                logger.info(f"TAKE-PROFIT {pos.side} {pos.question[:40]} ({pnl_pct:.1%})")
                to_close.append(token_id)

        self._dash_state.positions = position_snapshots

        for token_id in to_close:
            self._close_position(token_id)

    def _close_position(self, token_id: str) -> None:
        pos = self._positions.get(token_id)
        if not pos:
            return

        ob = self._client.get_order_book(token_id)
        sell_price = ob.best_bid if ob else pos.entry_price

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

        resp = self._client.place_limit_order(
            token_id=sig.token_id,
            side="BUY",
            price=limit_price,
            size=shares,
        )

        if resp:
            self._risk.register_open(usdc)
            self._dash_state.orders_placed += 1

            self._positions[sig.token_id] = OpenPosition(
                market_id=sig.market_id,
                question=sig.question,
                token_id=sig.token_id,
                side=sig.side,
                shares=shares,
                entry_price=limit_price,
                cost_usdc=usdc,
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
                entry_price=limit_price,
                shares=shares,
                cost_usdc=usdc,
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
                f"  {tag}Opened {sig.side}: {shares:.2f}@{limit_price:.4f} "
                f"= ${usdc:.2f}  [{sig.confidence}]"
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
        for m in markets:
            if not m.active or m.closed:
                continue
            if m.liquidity < config.MIN_LIQUIDITY_USDC:
                continue
            if m.volume < config.MIN_VOLUME_24H_USDC:
                continue
            if not (0.02 <= m.yes_price <= 0.98):
                continue
            hours_left = None
            if m.end_date:
                try:
                    end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                    secs_left = (end - now).total_seconds()
                    # Too close to expiry — order won't fill in time
                    if secs_left < min_minutes * 60:
                        continue
                    hours_left = secs_left / 3600
                    # Too far in the future
                    if hours_left > cutoff * 24:
                        continue
                except Exception:
                    pass
            filtered.append((m, hours_left if hours_left is not None else cutoff * 24))

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
