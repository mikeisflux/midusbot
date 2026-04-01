"""
Bot orchestrator — ties together the client, strategy, and risk manager
and runs the main trading loop.

Loop per iteration
──────────────────
1. Fetch all active markets from Gamma API.
2. Filter by liquidity / volume thresholds.
3. For each candidate market:
   a. Fetch order book (CLOB).
   b. Fetch price history (Gamma).
   c. Run strategy → TradeSignal or None.
4. For signals with sufficient edge:
   a. Ask risk manager for position size.
   b. Place limit order (or log dry-run).
5. Check existing open positions for stop-loss / take-profit.
6. Sleep until next iteration.
"""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from src.client import Market, PolymarketClient
from src.strategy import MomentumImbalanceStrategy, TradeSignal
from src.risk import RiskManager
import config


# ---------------------------------------------------------------------------
# Internal bookkeeping
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    market_id: str
    question: str
    token_id: str
    side: str        # "YES" | "NO"
    shares: float
    entry_price: float
    cost_usdc: float
    order_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class PolymarketBot:
    def __init__(self) -> None:
        self._client   = PolymarketClient()
        self._strategy = MomentumImbalanceStrategy()
        self._risk     = RiskManager()
        self._positions: dict[str, OpenPosition] = {}  # token_id → position
        self._running  = False

        # Graceful shutdown on SIGINT / SIGTERM
        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        mode_tag = "[DRY-RUN]" if config.DRY_RUN else "[LIVE]"
        logger.info(f"Polymarket bot starting — {mode_tag}")
        logger.info(
            f"Config: MAX_POS=${config.MAX_POSITION_USDC}  "
            f"MAX_EXPOSURE=${config.MAX_TOTAL_EXPOSURE_USDC}  "
            f"MIN_EDGE={config.MIN_EDGE:.1%}  "
            f"LOOP={config.LOOP_INTERVAL_SECONDS}s"
        )
        self._running = True

        while self._running:
            try:
                self._loop_once()
            except Exception as exc:
                logger.exception(f"Unhandled error in main loop: {exc}")

            if self._running:
                logger.info(f"Sleeping {config.LOOP_INTERVAL_SECONDS}s …\n")
                time.sleep(config.LOOP_INTERVAL_SECONDS)

        logger.info("Bot stopped.")

    # ------------------------------------------------------------------
    # Single loop iteration
    # ------------------------------------------------------------------

    def _loop_once(self) -> None:
        logger.info("── Loop start ──────────────────────────────────────────────")

        # 1. Check / manage existing positions
        self._manage_positions()

        # 2. Scan for new opportunities
        markets = self._client.get_markets()
        candidates = self._filter_markets(markets)
        logger.info(f"{len(candidates)}/{len(markets)} markets pass filters.")

        signals_found = 0
        trades_placed = 0

        for market in candidates:
            if not self._running:
                break

            # Don't double-up on a market we already hold
            if self._already_positioned(market):
                continue

            order_book  = self._client.get_order_book(market.yes_token.token_id)
            price_hist  = self._client.get_price_history(market.id)

            sig = self._strategy.analyse(market, order_book, price_hist)
            if sig is None:
                continue

            signals_found += 1
            placed = self._execute_signal(sig)
            if placed:
                trades_placed += 1

        logger.info(
            f"── Loop end — signals: {signals_found}  trades: {trades_placed}  "
            f"exposure: ${self._risk.total_exposure():.2f} ──"
        )

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_positions(self) -> None:
        if not self._positions:
            return

        to_close: list[str] = []

        for token_id, pos in self._positions.items():
            # Get current mid price from order book
            ob = self._client.get_order_book(token_id)
            current_price = ob.mid if ob else pos.entry_price

            pnl_usdc = pos.shares * (current_price - pos.entry_price)
            pnl_pct  = (current_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0

            logger.info(
                f"  Position {pos.side} {pos.question[:50]} | "
                f"entry={pos.entry_price:.3f}  now={current_price:.3f}  "
                f"PnL={pnl_usdc:+.2f} USDC ({pnl_pct:+.1%})"
            )

            if self._risk.should_stop_loss(pnl_pct):
                logger.warning(f"  → STOP-LOSS triggered ({pnl_pct:.1%})")
                to_close.append(token_id)
            elif self._risk.should_take_profit(pnl_pct):
                logger.info(f"  → TAKE-PROFIT triggered ({pnl_pct:.1%})")
                to_close.append(token_id)

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
            self._risk.register_close(pos.cost_usdc)
            del self._positions[token_id]
            logger.info(f"Closed position: {pos.side} {pos.question[:50]}")

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute_signal(self, sig: TradeSignal) -> bool:
        usdc_to_spend = self._risk.position_size(sig)
        if usdc_to_spend <= 0:
            return False

        # Buy slightly below fair value for a better fill
        limit_price = round(min(sig.fair_value, sig.market_price * 1.01), 4)
        limit_price = max(0.01, min(0.99, limit_price))
        shares      = self._risk.shares_from_usdc(usdc_to_spend, limit_price)

        if shares <= 0:
            return False

        resp = self._client.place_limit_order(
            token_id=sig.token_id,
            side="BUY",
            price=limit_price,
            size=shares,
        )

        if resp:
            self._risk.register_open(usdc_to_spend)
            self._positions[sig.token_id] = OpenPosition(
                market_id=sig.market_id,
                question=sig.question,
                token_id=sig.token_id,
                side=sig.side,
                shares=shares,
                entry_price=limit_price,
                cost_usdc=usdc_to_spend,
                order_id=resp.get("id") if isinstance(resp, dict) else None,
            )
            logger.info(
                f"  Opened {sig.side} position: {shares:.2f} shares @ {limit_price:.4f} "
                f"= ${usdc_to_spend:.2f} USDC  [{sig.confidence}]"
            )
            return True

        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _filter_markets(self, markets: list[Market]) -> list[Market]:
        return [
            m for m in markets
            if m.liquidity >= config.MIN_LIQUIDITY_USDC
            and m.volume   >= config.MIN_VOLUME_24H_USDC
            and not m.closed
            and m.active
            # Avoid extreme prices where there's no real market
            and 0.02 <= m.yes_price <= 0.98
        ]

    def _already_positioned(self, market: Market) -> bool:
        return (
            market.yes_token.token_id in self._positions
            or market.no_token.token_id in self._positions
        )

    def _shutdown(self, *_) -> None:
        logger.info("Shutdown signal received — stopping after current iteration…")
        self._running = False
