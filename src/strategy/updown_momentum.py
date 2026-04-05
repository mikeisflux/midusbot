"""
UpDownMomentumStrategy — oracle-lag / cold-start strategy.

Trades Polymarket "XRP Up or Down - HH:MM-HH:MM" style markets by measuring
Binance 60-second momentum before market makers reprice the YES/NO contracts.

Edge: When Binance shows BTC +0.08% in the last 5 minutes, the "Will BTC be
higher in 5 min?" YES token is still 0.50. That's a mispriced bet. Enter YES
at 0.50-0.51 before MMs catch up.
"""
from __future__ import annotations

import time

import numpy as np
from loguru import logger

from src.client import Market, OrderBook
from src import session_tracker as _st
from src.signals import (
    _PRICE_HISTORY,
    _fetch_price,
    _window_return,
    _consecutive_window_trend,
    _btc_leadership_signal,
    _exchange_pressure,
    _price_acceleration,
    _multitf_consensus,
    _get_trade_rate,
    _get_avg_trade_rate,
    _market_regime,
    _get_funding_rate,
    _get_oi_delta,
    _cross_exchange_divergence,
)
from src.fair_value import compute_updown_fair_value
from src.strategy.constants import (
    _MIN_MOMENTUM_PCT,
    _MIN_WINDOW_RETURN_PCT,
    _GLOBAL_THRESHOLD_CEIL,
    _DEFAULT_ASSET_THRESHOLDS,
    _ASSET_THRESHOLD_CEILS,
    _ASSET_THRESHOLD_FLOORS,
)
from src.strategy.signal_model import TradeSignal
from src.strategy.market_utils import (
    _detect_updown_market,
    _updown_window_mins,
    _market_seconds_into_window,
)
from src.strategy.analyst_cache import _load_analyst_params_cached
from src.strategy.prob_stats import (
    _bayesian_win_rate_mult,
    _normal_vol_mult,
    _markov_persistence_mult,
)
import config


class UpDownMomentumStrategy:
    """
    Trades Polymarket 5-min UpDown markets on Binance oracle lag.

    Guards (in order):
      1. Timing — must be 5–240s into the window
      2. Entry price — YES mid must be 0.46–0.54 (MMs haven't repriced)
      3. Book liquidity — bid=0.01/ask=0.99 means empty market, skip
      4. Window return threshold — per-asset configurable
      5. Conviction filter — recent trade rate vs. average
      6. Market regime — skip CHOPPY; skip if regime contradicts signal
      7. Momentum acceleration — skip if decelerating at t≥45s
      8. Multi-timeframe consensus — skip if timeframes disagree
      9. Consecutive window trend — skip if strong contra-trend
     10. BTC leadership — skip if BTC lead reverses direction
     11. Edge ≥ MIN_EDGE
    """

    def __init__(self, min_momentum_pct: float = _MIN_MOMENTUM_PCT) -> None:
        self.min_momentum_pct = min_momentum_pct

    def analyse(self, market: Market, order_book: OrderBook | None) -> TradeSignal | None:
        symbol = _detect_updown_market(market.question)
        if symbol is None:
            return None

        # ── Guard 1: Strict timing ────────────────────────────────────────────
        secs_in = _market_seconds_into_window(market)
        _ap = _load_analyst_params_cached()
        _max_secs_in = int(_ap.get("max_secs_in", 240))
        if secs_in is None or secs_in < 5:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] TIMING — secs_in={secs_in} < 5 or unknown → SKIP")
            return None
        window_mins = _updown_window_mins(market.question) or 5
        effective_max_secs = _max_secs_in if window_mins == 5 else int(window_mins * 60 * 0.80)
        if secs_in > effective_max_secs:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] TIMING — {secs_in:.0f}s into {window_mins}min window "
                f"(max={effective_max_secs}s) → SKIP (too late, oracle lag gone)"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] TIMING — {secs_in:.0f}s into {window_mins}min window "
            f"(max={effective_max_secs}s) → PASS"
        )

        # Time-of-day skip
        _tod_skip = _ap.get("time_of_day_skip", [])
        if _tod_skip and time.gmtime().tm_hour in _tod_skip:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] TOD-SKIP — UTC hour {time.gmtime().tm_hour} "
                f"in analyst block-list {_tod_skip} → SKIP"
            )
            return None

        # Price history warmup
        _min_hist = int(_ap.get("min_price_history_s", 30))
        live_price = _fetch_price(symbol)
        if live_price is None:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] PRICE-FEED — no live price from Binance WS → SKIP")
            return None
        _st.update(symbol, live_price)
        hist = _PRICE_HISTORY.get(symbol.upper(), [])
        if len(hist) < 2:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] WARMUP — price history empty (0 ticks) → SKIP")
            return None
        hist_span = hist[-1][1] - hist[0][1]
        if hist_span < _min_hist:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] WARMUP — only {hist_span:.0f}s of price history "
                f"(need {_min_hist}s) → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] WARMUP — {hist_span:.0f}s price history  "
            f"live_price={live_price:.4f} → PASS"
        )

        # ── Guard 2: Entry price must still be near 0.50 ─────────────────────
        mid = order_book.mid if order_book else market.yes_price
        if mid > config.ENTRY_PRICE_GUARD or mid < (1.0 - config.ENTRY_PRICE_GUARD):
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] ENTRY-GUARD — mid={mid:.3f} outside "
                f"[{1.0 - config.ENTRY_PRICE_GUARD:.2f}–{config.ENTRY_PRICE_GUARD:.2f}] "
                f"(MMs already repriced) → SKIP"
            )
            return None

        # ── Guard 2b: Order book liquidity check ──────────────────────────────
        if order_book and order_book.best_bid > 0 and order_book.best_ask > 0:
            _book_spread = order_book.best_ask - order_book.best_bid
            if _book_spread > 0.85:
                logger.debug(
                    f"[LOGIC:UPDOWN:{symbol}] BOOK-EMPTY — bid={order_book.best_bid:.3f} "
                    f"ask={order_book.best_ask:.3f} spread={_book_spread:.3f} "
                    f"(no real market, ask-guard would block anyway) → SKIP"
                )
                return None
        logger.debug(f"[LOGIC:UPDOWN:{symbol}] ENTRY-GUARD — mid={mid:.3f} within range → PASS")

        # Skip assets on analyst block-list
        if symbol.upper() in [s.upper() for s in _ap.get("skip_assets", [])]:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] SKIP-ASSETS — analyst blocked this asset → SKIP")
            return None

        # ── Window return + threshold ─────────────────────────────────────────
        window_return = _window_return(symbol, int(secs_in))
        _asset_thresholds = _ap.get("asset_thresholds", {})
        _sym = symbol.upper()
        if _sym in _asset_thresholds:
            _hard_floor = _ASSET_THRESHOLD_FLOORS.get(_sym, 0.00025)
            _hard_ceil  = _ASSET_THRESHOLD_CEILS.get(_sym, 0.00300)
            _sig_thresh = float(max(_hard_floor, min(_hard_ceil, _asset_thresholds[_sym])))
            _thresh_src = f"analyst ({_asset_thresholds[_sym]:.5%}, clamped floor={_hard_floor:.5%} ceil={_hard_ceil:.5%})"
        else:
            _global = float(max(
                _MIN_WINDOW_RETURN_PCT,
                min(_GLOBAL_THRESHOLD_CEIL, _ap.get("signal_threshold", _MIN_WINDOW_RETURN_PCT)),
            ))
            _sig_thresh = float(_DEFAULT_ASSET_THRESHOLDS.get(_sym, _global))
            _thresh_src = "default (asset default or global fallback)"

        if window_return is None:
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] WIN-RETURN — unavailable "
                f"(lookback={int(secs_in)}s) → SKIP"
            )
            return None
        _ratio = abs(window_return) / _sig_thresh if _sig_thresh > 0 else 0.0
        if abs(window_return) < _sig_thresh:
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] THRESHOLD — win_ret={window_return:+.4%} "
                f"thresh={_sig_thresh:.4%} ({_thresh_src}) ratio={_ratio:.2f}× → SKIP (below threshold)"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] THRESHOLD — win_ret={window_return:+.4%} "
            f"thresh={_sig_thresh:.4%} ({_thresh_src}) ratio={_ratio:.2f}× → PASS"
        )

        # ── Conviction filter ─────────────────────────────────────────────────
        _rate_30s = _get_trade_rate(symbol, window_secs=30)
        _rate_avg = _get_avg_trade_rate(symbol)
        if _rate_avg > 0 and _rate_30s > 0:
            _rate_ratio = _rate_30s / _rate_avg
            if _rate_ratio < 0.5:
                logger.info(
                    f"[LOGIC:UPDOWN:{symbol}] CONVICTION — rate={_rate_30s:.2f}/s "
                    f"avg={_rate_avg:.2f}/s ratio={_rate_ratio:.2f}× (need ≥0.5×) → SKIP (low conviction)"
                )
                return None
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] CONVICTION — rate={_rate_30s:.2f}/s "
                f"avg={_rate_avg:.2f}/s ratio={_rate_ratio:.2f}× → PASS"
            )

        # ── Market regime filter ──────────────────────────────────────────────
        regime = _market_regime(symbol)
        signal_dir = "UP" if window_return > 0 else "DOWN"
        if regime == "CHOPPY":
            logger.info(
                f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} signal={signal_dir} → SKIP (choppy = 50% noise)"
            )
            return None
        if regime == "TRENDING_UP" and signal_dir == "DOWN":
            logger.info(f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} contradicts DOWN signal → SKIP")
            return None
        if regime == "TRENDING_DOWN" and signal_dir == "UP":
            logger.info(f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} contradicts UP signal → SKIP")
            return None
        logger.debug(f"[LOGIC:UPDOWN:{symbol}] REGIME — {regime} compatible with {signal_dir} → PASS")

        # ── Mean reversion check ──────────────────────────────────────────────
        _is_extreme_move = abs(window_return) >= 0.003  # 0.3%
        if _is_extreme_move:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] EXTREME-MOVE — win_ret={window_return:+.4%} ≥ 0.3% "
                f"→ confidence will be capped at MEDIUM unless 3+ confirmations"
            )

        # ── Funding rate confirmation ─────────────────────────────────────────
        _funding = _get_funding_rate(symbol)
        if _funding is not None:
            _funding_dir = "UP" if _funding > 0 else "DOWN"
            _funding_aligns = _funding_dir == signal_dir
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] FUNDING — rate={_funding:.6f} dir={_funding_dir} "
                f"signal={signal_dir} → {'CONFIRMS' if _funding_aligns else 'CONTRADICTS'}"
            )
        else:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] FUNDING — no data (neutral)")

        # ── OI delta confirmation ─────────────────────────────────────────────
        _oi_delta = _get_oi_delta(symbol)
        _oi_confirms = _oi_delta is not None and _oi_delta > 0.001
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] OI-DELTA — delta={_oi_delta} "
            f"→ {'CONFIRMS (rising OI = conviction)' if _oi_confirms else 'no confirmation'}"
        )

        # ── Cross-exchange divergence ─────────────────────────────────────────
        _ce_div = _cross_exchange_divergence(symbol)
        _ce_confirms = abs(_ce_div) > 0.0002 and ((_ce_div > 0) == (window_return > 0))
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] CE-DIV — coinbase_vs_binance={_ce_div:+.4%} "
            f"→ {'CONFIRMS arb pressure' if _ce_confirms else 'no confirmation'}"
        )

        # ── Momentum acceleration check ───────────────────────────────────────
        if secs_in >= 45:
            accel = _price_acceleration(symbol)
            if accel is not None:
                _accel_ok = (accel > 0) == (window_return > 0)
                if not _accel_ok:
                    logger.info(
                        f"[LOGIC:UPDOWN:{symbol}] ACCEL — win_ret={window_return:+.4%} "
                        f"accel={accel:+.4%} DECELERATING at t={secs_in:.0f}s → SKIP (mean reversion likely)"
                    )
                    return None
                logger.debug(
                    f"[LOGIC:UPDOWN:{symbol}] ACCEL — win_ret={window_return:+.4%} "
                    f"accel={accel:+.4%} still accelerating → PASS"
                )
        else:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] ACCEL — t={secs_in:.0f}s < 45s, check skipped")

        # ── Multi-timeframe consensus ─────────────────────────────────────────
        consensus_score, consensus_conf = _multitf_consensus(symbol)
        if consensus_conf not in ("NONE",) and consensus_score != 0.0:
            consensus_dir = "UP" if consensus_score > 0 else "DOWN"
            signal_dir    = "UP" if window_return > 0 else "DOWN"
            if consensus_dir != signal_dir:
                logger.info(
                    f"[LOGIC:UPDOWN:{symbol}] MULTITF — score={consensus_score:+.2f} conf={consensus_conf} "
                    f"consensus={consensus_dir} vs signal={signal_dir} → SKIP (timeframes disagree)"
                )
                return None
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] MULTITF — score={consensus_score:+.2f} conf={consensus_conf} "
                f"consensus={consensus_dir} agrees with signal → PASS"
            )
        else:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] MULTITF — score={consensus_score:+.2f} conf={consensus_conf} "
                f"→ no strong consensus, proceeding"
            )

        # ── Consecutive window trend ──────────────────────────────────────────
        trend_score = _consecutive_window_trend(symbol)
        trend_dir_ok = True
        _min_trend = float(_ap.get("min_trend_score", 0.0))
        if trend_score is not None:
            trend_direction  = "UP" if trend_score > 0 else "DOWN"
            signal_direction = "UP" if window_return > 0 else "DOWN"
            if trend_direction != signal_direction and abs(trend_score) >= 0.5:
                logger.debug(
                    f"[LOGIC:UPDOWN:{symbol}] CW-TREND — score={trend_score:+.2f} "
                    f"trend={trend_direction} contradicts signal={signal_direction} (|score|≥0.5) → SKIP"
                )
                return None
            trend_dir_ok = trend_direction == signal_direction
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] CW-TREND — score={trend_score:+.2f} "
                f"trend={trend_direction} signal={signal_direction} "
                f"aligned={'YES' if trend_dir_ok else 'NO (weak, not blocking)'}"
            )
        else:
            logger.debug(f"[LOGIC:UPDOWN:{symbol}] CW-TREND — no data (neutral, not blocking)")
        if _min_trend > 0 and (trend_score is None or abs(trend_score) < _min_trend):
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] MIN-TREND — score={trend_score} < required {_min_trend} → SKIP"
            )
            return None

        # ── BTC leadership ────────────────────────────────────────────────────
        btc_lead = _btc_leadership_signal(symbol)
        combined = window_return + btc_lead
        if btc_lead != 0.0 and (combined > 0) != (window_return > 0):
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] BTC-LEAD — btc={btc_lead:+.4%} "
                f"combined={combined:+.4%} REVERSES signal direction → SKIP"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] BTC-LEAD — btc={btc_lead:+.4%} "
            f"win_ret={window_return:+.4%} combined={combined:+.4%} → PASS "
            f"direction={'UP' if combined > 0 else 'DOWN'}"
        )

        direction = "UP" if combined > 0 else "DOWN"

        # ── Fair value + edge ─────────────────────────────────────────────────
        fair_prob = compute_updown_fair_value(window_return, trend_score, trend_dir_ok)

        if direction == "UP":
            side = "YES"
            token = market.yes_token
            mkt_price = mid
        else:
            side = "NO"
            token = market.no_token
            mkt_price = 1.0 - mid

        fair_value = float(np.clip(fair_prob, 0.01, 0.99))
        edge = fair_value - mkt_price

        if edge < config.MIN_EDGE:
            logger.debug(
                f"[LOGIC:UPDOWN:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
                f"edge={edge:+.3f} < MIN_EDGE={config.MIN_EDGE:.3f} → SKIP (insufficient edge)"
            )
            return None
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] EDGE — fair={fair_value:.3f} mkt={mkt_price:.3f} "
            f"edge={edge:+.3f} ≥ MIN_EDGE={config.MIN_EDGE:.3f} → PASS"
        )

        # ── Confidence scoring ────────────────────────────────────────────────
        _confirmations = sum([
            abs(window_return) >= 0.003,                                      # strong move
            trend_score is not None and abs(trend_score) >= 0.75,            # strong trend
            _oi_confirms,                                                      # OI rising
            _ce_confirms,                                                      # cross-exchange confirms
            _funding is not None and ((_funding > 0) == (window_return > 0)), # funding aligns
        ])
        if _is_extreme_move and _confirmations < 3:
            confidence = "MEDIUM"
        elif _confirmations >= 3:
            confidence = "HIGH"
        elif _confirmations >= 1:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] CONFIDENCE — confirmations={_confirmations}/5 "
            f"(strong_move={abs(window_return)>=0.003}, trend={trend_score is not None and abs(trend_score or 0)>=0.75}, "
            f"oi={_oi_confirms}, ce={_ce_confirms}, "
            f"funding={_funding is not None and ((_funding>0)==(window_return>0))}) "
            f"extreme={_is_extreme_move} → {confidence}"
        )
        pressure = _exchange_pressure(symbol)

        # Relative strength: how much does this signal exceed its own threshold?
        rel_strength = abs(window_return) / _sig_thresh if _sig_thresh > 0 else 0.0

        # Consecutive window persistence boost
        if trend_score is not None and abs(trend_score) >= 0.6:
            persistence_boost = 1.0 + abs(trend_score) * 0.5
            rel_strength *= persistence_boost

        # prefer_assets boost
        if symbol.upper() in [s.upper() for s in _ap.get("prefer_assets", [])]:
            rel_strength *= 1.5
            logger.debug(f"[UPDOWN] {symbol} prefer_assets boost → rel_strength={rel_strength:.2f}×")

        # ── Advanced probability modifiers ────────────────────────────────────
        _bayes_mult  = _bayesian_win_rate_mult(symbol)
        _vol_mult    = _normal_vol_mult(symbol, window_return)
        _markov_mult = _markov_persistence_mult(symbol, direction)
        _prob_mult   = _bayes_mult * _vol_mult * _markov_mult
        _rel_before  = rel_strength
        rel_strength *= _prob_mult
        logger.debug(
            f"[LOGIC:UPDOWN:{symbol}] PROB-MULTS — "
            f"bayes={_bayes_mult:.3f}×  vol={_vol_mult:.3f}×  markov={_markov_mult:.3f}×  "
            f"combined={_prob_mult:.3f}×  rel_strength: {_rel_before:.3f} → {rel_strength:.3f}"
        )

        logger.info(
            f"[UPDOWN] {symbol} {direction}  "
            f"win_ret={window_return:+.4%}  thresh={_sig_thresh:.4%}  rel={rel_strength:.2f}×  "
            f"trend={f'{trend_score:+.2f}' if trend_score is not None else 'N/A'}  btc={btc_lead:+.4%}  "
            f"fair={fair_value:.3f}  mkt={mkt_price:.3f}  edge={edge:+.3f}  "
            f"t={secs_in:.0f}s  → {side} [{confidence}]  \"{market.question[:45]}\""
        )

        _st.update(symbol, live_price, signal_direction=direction)

        return TradeSignal(
            market_id=market.id,
            question=market.question,
            side=side,
            token_id=token.token_id,
            market_price=mkt_price,
            fair_value=fair_value,
            edge=edge,
            signal=float(np.clip(abs(window_return) * 200, 0.0, 1.0)),
            confidence=confidence,
            momentum_signal=float(window_return),
            imbalance_signal=float(pressure),
            is_latency_arb=False,
            secs_into_window=float(secs_in),
            rel_strength=rel_strength,
            win_mins=window_mins,
        )
