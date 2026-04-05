"""
TrendFollowStrategy — per-asset win-streak direction tracker.

Tracks which direction (UP / DOWN) each asset has been winning in across
successive 5-minute UpDown markets.

  WIN  → keep betting the same direction (trend continuing)
  LOSS → flip to the opposite direction (trend reversed)

We enter near the START of each new 5-minute window when the price is
still close to 0.50, before the market has priced in the direction.
"""
from __future__ import annotations

import numpy as np
from loguru import logger

from src.client import Market, OrderBook
from src import session_tracker as _st
from src.signals import (
    _fetch_price,
    _window_return,
    _consecutive_window_trend,
    _btc_leadership_signal,
)
from src.fair_value import compute_trendfollow_fair_value
from src.strategy.constants import _MIN_WINDOW_RETURN_PCT
from src.strategy.signal_model import TradeSignal
from src.strategy.market_utils import (
    _detect_updown_market,
    _market_seconds_into_window,
)
from src.strategy.prob_stats import (
    _bayesian_win_rate_mult,
    _markov_persistence_mult,
    _binomial_streak_confidence,
)
import config


class TrendFollowStrategy:
    """
    Follows the established per-asset UpDown trend direction.

    State is maintained by a TrendTracker instance (passed in).
    Signals are only generated when:
      - The tracker has history for the asset (direction is known).
      - The YES mid price is still near 0.50 (oracle lag still available).
      - Binance window return agrees with the streak direction.
      - Edge exceeds MIN_EDGE.
    """

    FAIR_VALUE_BASE = 0.63   # baseline fair value for trend continuation
    STREAK_BONUS    = 0.02   # +2% per streak step, capped at 0.80

    def __init__(self, tracker) -> None:
        self._tracker = tracker

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        symbol = _detect_updown_market(market.question)
        if symbol is None:
            return None

        direction = self._tracker.get_direction(symbol)
        if direction is None:
            logger.debug(f"[LOGIC:TREND:{symbol}] DIRECTION — no history in TrendTracker → SKIP (cold start)")
            return None

        # ── Strict timing ─────────────────────────────────────────────────────
        secs_in = _market_seconds_into_window(market)
        if secs_in is None or secs_in < 5 or secs_in > 240:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] TIMING — secs_in={secs_in} (need 5–240s) → SKIP"
            )
            return None

        streak = self._tracker.get_streak(symbol)
        mid = order_book.mid if order_book else market.yes_price
        logger.debug(
            f"[LOGIC:TREND:{symbol}] STATE — direction={direction} streak={streak} "
            f"mid={mid:.3f} t={secs_in:.0f}s"
        )

        # ── Price guard: YES mid must still be near 0.50 ──────────────────────
        # This checks the YES mid directly — not the side-adjusted mkt_price.
        # A NO trade where YES=0.69 gives mkt_price=0.31, which would pass a
        # naive mkt_price>0.60 check even though MMs have already repriced by 19¢.
        # Using ENTRY_PRICE_GUARD on the YES mid is symmetrical for both sides.
        _guard = config.ENTRY_PRICE_GUARD  # default 0.54
        if mid > _guard or mid < (1.0 - _guard):
            logger.debug(
                f"[LOGIC:TREND:{symbol}] PRICE-GUARD — mid={mid:.3f} outside "
                f"[{1.0 - _guard:.2f}–{_guard:.2f}] (MMs already repriced) → SKIP"
            )
            return None

        if direction == "UP":
            side  = "YES"
            token = market.yes_token
            mkt_price = mid
        else:
            side  = "NO"
            token = market.no_token
            mkt_price = 1.0 - mid

        # ── Book liquidity guard ──────────────────────────────────────────────
        if order_book and order_book.best_bid > 0 and order_book.best_ask > 0:
            _spread = order_book.best_ask - order_book.best_bid
            if _spread > 0.85:
                logger.debug(
                    f"[LOGIC:TREND:{symbol}] BOOK-EMPTY — bid={order_book.best_bid:.3f} "
                    f"ask={order_book.best_ask:.3f} spread={_spread:.3f} → SKIP (no liquidity)"
                )
                return None

        logger.debug(
            f"[LOGIC:TREND:{symbol}] PRICE-GUARD — mid={mid:.3f} within "
            f"[{1.0 - _guard:.2f}–{_guard:.2f}] → PASS"
        )

        # ── Window-relative confirmation ──────────────────────────────────────
        # Current Binance move must agree with streak direction.
        window_return = _window_return(symbol, int(secs_in))
        if window_return is None:
            logger.debug(f"[LOGIC:TREND:{symbol}] WINDOW-CONFIRM — window_return unavailable → SKIP")
            return None
        window_dir = "UP" if window_return > 0 else "DOWN"
        if window_dir != direction:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] WINDOW-CONFIRM — win_ret={window_return:+.4%} "
                f"dir={window_dir} contradicts streak direction={direction} → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:TREND:{symbol}] WINDOW-CONFIRM — win_ret={window_return:+.4%} "
            f"dir={window_dir} agrees with streak={direction} → PASS"
        )

        btc_lead = _btc_leadership_signal(symbol)
        combined = window_return + btc_lead
        logger.debug(
            f"[LOGIC:TREND:{symbol}] BTC-LEAD — btc={btc_lead:+.4%} "
            f"win_ret={window_return:+.4%} combined={combined:+.4%}"
        )

        # ── Consecutive-window trend boost ────────────────────────────────────
        cw_trend = _consecutive_window_trend(symbol)
        cw_boost = 0
        if cw_trend is not None:
            cw_dir = "UP" if cw_trend > 0 else "DOWN"
            if cw_dir == direction and abs(cw_trend) >= 0.5:
                cw_boost = 1
        logger.debug(
            f"[LOGIC:TREND:{symbol}] CW-TREND — score={cw_trend} boost={cw_boost} "
            f"(+1 to streak if recent windows align)"
        )

        # ── Fair value + edge ─────────────────────────────────────────────────
        fair_value = compute_trendfollow_fair_value(streak, cw_boost)
        edge = fair_value - mkt_price
        if edge < config.MIN_EDGE:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
                f"edge={edge:+.3f} < MIN_EDGE={config.MIN_EDGE:.3f} → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:TREND:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
            f"edge={edge:+.3f} ≥ MIN_EDGE={config.MIN_EDGE:.3f} → PASS"
        )

        # ── Binomial significance gate ────────────────────────────────────────
        confidence = _binomial_streak_confidence(streak)
        _p_luck = 0.5 ** streak
        logger.debug(
            f"[LOGIC:TREND:{symbol}] BINOMIAL — streak={streak} "
            f"P(luck)={_p_luck:.4f} → confidence={confidence}"
        )
        if confidence == "LOW" and abs(window_return) < _MIN_WINDOW_RETURN_PCT * 2:
            logger.debug(
                f"[LOGIC:TREND:{symbol}] LOW-CONF-GATE — LOW confidence + weak win_ret={window_return:+.4%} "
                f"(need ≥{_MIN_WINDOW_RETURN_PCT*2:.4%}) → SKIP (too risky on fresh flip)"
            )
            return None

        # ── Probability multipliers ───────────────────────────────────────────
        _tf_bayes  = _bayesian_win_rate_mult(symbol)
        _tf_markov = _markov_persistence_mult(symbol, direction)
        _tf_prob   = _tf_bayes * _tf_markov
        _tf_rel_strength = (edge / config.MIN_EDGE) * (1.0 + streak * 0.1) * _tf_prob
        logger.debug(
            f"[LOGIC:TREND:{symbol}] PROB-MULTS — "
            f"bayes={_tf_bayes:.3f}×  markov={_tf_markov:.3f}×  combined={_tf_prob:.3f}×  "
            f"rel_strength={_tf_rel_strength:.3f} "
            f"(base={edge/config.MIN_EDGE:.2f}× × streak_factor={1.0+streak*0.1:.2f}× × prob={_tf_prob:.3f}×)"
        )

        _cw_str = f"{cw_trend:+.2f}" if cw_trend is not None else "N/A"
        logger.info(
            f"[TREND-FOLLOW] {symbol} {direction}  streak={streak}  cw={_cw_str}  "
            f"win_ret={window_return:+.4%}  "
            f"fair={fair_value:.2f}  mkt={mkt_price:.2f}  edge={edge:+.2f}  "
            f"bayes={_tf_bayes:.2f}×  markov={_tf_markov:.2f}×  "
            f"t={secs_in:.0f}s  [{confidence}]  \"{market.question[:45]}\""
        )

        _tf_live = _fetch_price(symbol) or mid
        _st.update(symbol, _tf_live, signal_direction=direction)
        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_price,
            fair_value=fair_value,
            edge=edge,
            signal=edge,
            confidence=confidence,
            momentum_signal=float(streak),
            imbalance_signal=float(combined),
            is_latency_arb=False,
            rel_strength=_tf_rel_strength,
        )
