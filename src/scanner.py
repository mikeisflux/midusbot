"""
Market scanning mixin for PolymarketBot.

Contains: main loop body (_loop_once), market filtering, trading hours check,
and burst-mode sleep logic (_smart_sleep, _secs_until_next_window).
"""
from __future__ import annotations

import time

from loguru import logger

from src.strategy import _detect_updown_market, _updown_window_mins, _market_seconds_into_window
import config


class ScannerMixin:
    _off_hours_alerted: bool = False

    def _filter_markets(self, markets) -> list:
        from datetime import datetime, timezone
        cutoff = config.MAX_DAYS_TO_RESOLUTION

        now = datetime.now(timezone.utc)
        min_minutes = config.MIN_MINUTES_TO_RESOLUTION
        filtered = []

        n_inactive = n_price = n_liquidity = n_volume = n_toosoon = n_toolate = n_nodate = n_expired = 0
        n_updown_seen = n_updown_pass = 0
        n_ud_expired = n_ud_toosoon = n_ud_price = 0

        for m in markets:
            if not m.active or m.closed:
                n_inactive += 1
                continue

            is_updown = _detect_updown_market(m.question) is not None
            if is_updown:
                n_updown_seen += 1

            if is_updown:
                # Allow prices down to 0.001 (0.1¢) — penny bets are valid entries
                if not (0.001 <= m.yes_price <= 0.999):
                    n_price += 1; n_ud_price += 1; continue
            else:
                if m.liquidity < config.MIN_LIQUIDITY_USDC:
                    n_liquidity += 1; continue
                if not (0.005 <= m.yes_price <= 0.995):
                    n_price += 1; continue

            hours_left = None
            if m.end_date:
                try:
                    end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                    secs_left = (end - now).total_seconds()
                    if secs_left < -300:
                        n_expired += 1
                        if is_updown:
                            n_ud_expired += 1
                        continue
                    if is_updown:
                        win_secs = (_updown_window_mins(m.question) or 5) * 60
                        if secs_left > win_secs:
                            continue
                        if secs_left < 90:
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
                f"too_soon={n_toosoon} too_late={n_toolate}(non-UD>48h) "
                f"no_date={n_nodate} expired={n_expired}"
            )

        filtered.sort(key=lambda x: x[1])
        return [m for m, _ in filtered]

    def _check_trading_hours(self) -> None:
        from datetime import datetime as _dt
        now   = _dt.now()
        hour  = now.hour
        start = config.TRADING_HOUR_START
        end   = config.TRADING_HOUR_END

        if start <= hour < end:
            if self._off_hours_alerted:
                logger.info(f"[HOURS] Trading window open ({start:02d}:00 – {end:02d}:00). Resuming.")
                from src.utils import alerter
                alerter.send(f"Trading hours resumed ({start}:00 – {end}:00 CT). Scanning markets.", level="info")
                self._off_hours_alerted = False
            return

        if not self._off_hours_alerted:
            wake_time = now.replace(hour=start, minute=0, second=0, microsecond=0)
            if hour >= end:
                from datetime import timedelta as _td
                wake_time += _td(days=1)
            logger.info(
                f"[HOURS] Off-hours ({hour:02d}:xx). No 5-min markets until {start:02d}:00. "
                f"Sleeping until {wake_time.strftime('%H:%M')}."
            )
            from src.utils import alerter
            alerter.send(
                f"Off-hours ({hour}:{now.minute:02d} CT) — no 5-min markets. "
                f"Sleeping until {start}:00 CT.",
                level="info",
            )
            self._off_hours_alerted = True

        time.sleep(60)

    @staticmethod
    def _secs_until_next_window(window_mins: int = 5) -> float:
        now = time.time()
        window_secs = window_mins * 60
        return window_secs - (now % window_secs)

    def _start_penny_watcher(self) -> None:
        """
        Dedicated background thread that scans BTC orderbooks every 2 seconds
        for penny-price tokens (≤$0.01). Runs independently of the main loop
        so we never miss a penny window due to 15-second loop timing.
        Uses _btc_penny_token_cache populated by the main loop each iteration.
        """
        import threading

        if getattr(self, "_penny_watcher_started", False):
            return
        self._penny_watcher_started = True
        self._btc_penny_token_cache: list = []  # [(side, token_id, market_id, question)]

        def _watch():
            _PENNY_MAX = 0.01
            _MAX_BET   = 5.0
            from src.bot import OpenPosition
            while getattr(self, "_running", True):
                try:
                    for _entry in list(self._btc_penny_token_cache):
                        # 6-tuple: (side, token_id, opp_token_id, mkt_id, question, end_date)
                        if len(_entry) == 6:
                            _side, _token_id, _opp_token_id, _mkt_id, _question, _end_date = _entry
                        else:
                            continue  # stale cache format, skip
                        # Skip if market already closed
                        if _end_date:
                            try:
                                from datetime import datetime as _wdt2, timezone as _wtz2
                                _end_ts = _wdt2.fromisoformat(_end_date.replace("Z", "+00:00"))
                                if (_end_ts - _wdt2.now(_wtz2.utc)).total_seconds() <= 0:
                                    continue
                            except Exception:
                                pass
                        if _token_id in self._positions:
                            continue
                        _ob  = self._client.get_order_book(_token_id)
                        _ask = _ob.best_ask if _ob else None
                        if _ask is None or _ask > _PENNY_MAX:
                            continue
                        _ask    = max(_ask, 0.001)
                        _shares = int(_MAX_BET / _ask)
                        if _shares < config.MIN_ORDER_SHARES:
                            continue
                        logger.info(
                            f"[BTC-PENNY-FAST] {_side} @ ${_ask:.4f} — buying {_shares} shares "
                            f"for ${_MAX_BET:.2f}  {_question[:45]}"
                        )
                        resp = self._client.place_limit_order(
                            token_id=_token_id, side="BUY",
                            price=round(_ask, 4), size=float(_shares), fok=False,
                        )
                        if not resp:
                            continue
                        _cost = _shares * _ask
                        _sell_ts = time.time() + 30.0
                        self._positions[_token_id] = OpenPosition(
                            market_id=_mkt_id, question=_question,
                            token_id=_token_id, side=_side,
                            shares=float(_shares), entry_price=_ask, cost_usdc=_cost,
                            confidence="PENNY", entry_time=time.time(),
                            strategy="btc_penny", sell_at_ts=_sell_ts,
                        )
                        self._save_positions()
                        self._risk.record_open(cost_usdc=_cost)
                        self._dash_state.orders_placed += 1
                        self._learner.record_open(
                            market_id=_mkt_id, token_id=_token_id, side=_side,
                            question=_question, entry_price=_ask, shares=float(_shares),
                            cost_usdc=_cost, confidence="PENNY",
                            dry_run=config.DRY_RUN, strategy="btc_penny",
                        )
                        # Immediately buy opposing side ($5) — hold to resolution
                        if _opp_token_id and _opp_token_id not in self._positions:
                            try:
                                _opp_side = "NO" if _side == "YES" else "YES"
                                _opp_ob   = self._client.get_order_book(_opp_token_id)
                                _opp_ask  = _opp_ob.best_ask if _opp_ob else None
                                if _opp_ask and 0 < _opp_ask < 1.0:
                                    _opp_shares = max(1, int(_MAX_BET / _opp_ask))
                                    _opp_resp = self._client.place_limit_order(
                                        token_id=_opp_token_id, side="BUY",
                                        price=round(_opp_ask, 4),
                                        size=float(_opp_shares), fok=False,
                                    )
                                    if _opp_resp:
                                        _opp_cost = _opp_shares * _opp_ask
                                        self._positions[_opp_token_id] = OpenPosition(
                                            market_id=_mkt_id, question=_question,
                                            token_id=_opp_token_id, side=_opp_side,
                                            shares=float(_opp_shares), entry_price=_opp_ask,
                                            cost_usdc=_opp_cost, confidence="PENNY-HEDGE",
                                            entry_time=time.time(), strategy="btc_penny_hedge",
                                        )
                                        self._save_positions()
                                        self._risk.record_open(cost_usdc=_opp_cost)
                                        self._dash_state.orders_placed += 1
                                        logger.info(
                                            f"[BTC-PENNY-HEDGE] {_opp_side} @ ${_opp_ask:.4f} "
                                            f"— {_opp_shares} shares  {_question[:45]}"
                                        )
                            except Exception as _he:
                                logger.debug(f"[BTC-PENNY-HEDGE] hedge order failed: {_he}")
                except Exception as exc:
                    logger.debug(f"[BTC-PENNY-FAST] watcher error: {exc}")
                time.sleep(2.0)

        t = threading.Thread(target=_watch, daemon=True, name="btc-penny-watcher")
        t.start()
        logger.info("[BTC-PENNY-FAST] Dedicated penny watcher started — scanning every 2s")

    def _smart_sleep(self) -> None:
        BURST_LEAD_SECS     = 6
        BURST_LOOPS         = 4
        BURST_INTERVAL_SECS = 1.5

        secs_to_boundary = self._secs_until_next_window(5)

        if secs_to_boundary <= 30:
            logger.debug(f"[PRE-WINDOW] {secs_to_boundary:.1f}s to next window — watching for pre-entry signal")

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

    def _loop_once(self) -> None:
        from datetime import datetime
        from src.strategy import _fetch_price

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

        # Retrain ML classifier every 24h (non-blocking)
        if self._dash_state.loop_count % 3600 == 0:
            try:
                from src.ml_classifier import get_classifier
                get_classifier()  # triggers retrain if stale
            except Exception:
                pass

        # Async parallel price fetching — scan all assets concurrently instead of
        # sequentially (was: asset 7 signal was 3-5s stale by the time we got to it)
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        _ASSETS_TO_FETCH = ("BTC", "XRP", "ETH", "SOL", "DOGE", "BNB")  # HYPE removed: coin-flip win rate (188W/190L)
        with ThreadPoolExecutor(max_workers=len(_ASSETS_TO_FETCH), thread_name_prefix="pricefetch") as _pool:
            _futures = {_pool.submit(_fetch_price, sym): sym for sym in _ASSETS_TO_FETCH}
            for _fut in _as_completed(_futures):
                _fut.result()  # surface exceptions if any

        if self._dash_state.loop_count % 4 == 1:
            from src.signals import _PRICE_CACHE, _PRICE_HISTORY, _LAST_WS_TICK
            import time as _time
            _now = _time.time()
            parts = []
            silent_assets = []
            for sym in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
                cached    = _PRICE_CACHE.get(sym)
                hist      = _PRICE_HISTORY.get(sym, [])
                ticks     = len(hist)
                span      = int(hist[-1][1] - hist[0][1]) if len(hist) >= 2 else 0
                last_tick = _LAST_WS_TICK.get(sym)
                price_str = f"${cached[0]:,.2f}" if cached else "-"
                if last_tick and (_now - last_tick) < 120:
                    status = f"✓{ticks}t/{span}s"
                elif ticks >= 5 and span >= 90:
                    status = f"⚠REST{ticks}t/{span}s"  # has data but WS silent > 2 min
                    silent_assets.append(sym)
                else:
                    status = f"⏳{ticks}t/{span}s"
                    if ticks == 0:
                        silent_assets.append(sym)
                parts.append(f"{sym}={price_str}({status})")
            logger.info("Feed: " + "  ".join(parts))
            if silent_assets:
                logger.warning(f"Feed: WS silent for {silent_assets} — using REST/stale fallback")
            if len(silent_assets) >= 5:
                logger.warning(
                    f"Feed: {len(silent_assets)}/7 assets silent — WS may be down. "
                    f"Consider restarting WebSocket connection."
                )

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
        _updown_assets_held: set[str] = set()
        for pos in self._positions.values():
            sym = _detect_updown_market(pos.question)
            if sym:
                _updown_assets_held.add(sym)

        # Cross-window correlation cooldown: block correlated assets for 1 full
        # 5-min window after a bet (300s). BTC/ETH/BNB/SOL/XRP/DOGE are highly
        # correlated — concurrent bets are ~6× leveraged on the same direction.
        _CORRELATED = {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE"}
        _cooldown_map: dict[str, float] = getattr(self, "_asset_last_bet", {})
        if not hasattr(self, "_asset_last_bet"):
            self._asset_last_bet: dict[str, float] = {}
            _cooldown_map = self._asset_last_bet
        _cooled_out: set[str] = set()
        for _sym, _ts in _cooldown_map.items():
            if time.time() - _ts < config.COOLDOWN_SECS:
                _cooled_out.add(_sym)

        # Collect all valid signals, then execute only the single best.
        # BTC/ETH/SOL/XRP/DOGE/BNB are 90%+ correlated — betting all at once
        # is 6× leverage on one direction, not diversification.
        pending_signals: list[tuple] = []  # (sig, _asset)

        # In CHAINLINK_ONLY mode, skip all oracle-lag (T=0) signal evaluation.
        # Entries come exclusively from Chainlink oracle confirmation at T≈270s.
        if getattr(config, "CHAINLINK_ONLY", False):
            logger.debug("[CHAINLINK-ONLY] Oracle-lag evaluation skipped — Chainlink watches active")
            candidates = []

        for market in candidates:
            if not self._running:
                break

            _asset = _detect_updown_market(market.question)
            if _asset is None:
                continue

            _win_mins = _updown_window_mins(market.question)
            if _win_mins != 5:  # 5-min UpDown only — oracle lag edge is weaker on longer windows
                continue

            if self._already_positioned(market):
                continue
            if (market.yes_token.token_id in self._positions or
                    market.no_token.token_id in self._positions):
                continue
            if _asset in _updown_assets_held:
                continue
            # Block correlated assets if one was bet recently (cross-window cooldown)
            if _asset in _CORRELATED and _asset in _cooled_out:
                logger.debug(f"[COOLDOWN] Skipping {_asset} — cooldown active ({config.COOLDOWN_SECS}s)")
                continue

            _secs = _market_seconds_into_window(market)
            if _secs is None or _secs < 2 or _secs > 240:
                continue

            ob = self._client.get_order_book(market.yes_token.token_id)

            # Skip markets with completely empty order books (MMs absent AND price is stale)
            if ob is None or (not ob.bids and not ob.asks):
                logger.debug(f"[STALE] {market.question[:40]} — empty order book, skipping")
                continue

            _mid_now = ob.mid if ob else market.yes_price

            # Competing bot indicator: if mid is already repriced within 5s of window open
            if _secs < 5 and ob:
                if _mid_now > 0.54 or _mid_now < 0.46:
                    logger.debug(
                        f"[BOT-DETECT] {market.question[:40]} — mid={_mid_now:.3f} "
                        f"repriced in <5s: competing bots active"
                    )
                    continue

            # ── Bounce-trap guard ─────────────────────────────────────────────
            # Track the min/max mid seen per market per 5-minute window.
            # If the mid was previously extreme (>0.14 from 0.50) but has since
            # bounced back inside the entry guard, MMs already had strong conviction
            # in one direction — entering the "bounce" is a trap.
            # XRP example: mid went 0.50→0.31→0.52. Bot entered UP at 0.52 and lost -30%.
            if not hasattr(self, "_window_mid_range"):
                self._window_mid_range: dict = {}  # token_id → (win_ts, min_mid, max_mid)
            _win_ts = int(time.time() // 300) * 300
            _tok_key = market.yes_token.token_id
            _prev_range = self._window_mid_range.get(_tok_key, (0, _mid_now, _mid_now))
            if _prev_range[0] != _win_ts:
                # New window — reset tracking for this market
                self._window_mid_range[_tok_key] = (_win_ts, _mid_now, _mid_now)
            else:
                self._window_mid_range[_tok_key] = (
                    _win_ts,
                    min(_prev_range[1], _mid_now),
                    max(_prev_range[2], _mid_now),
                )
            _wmin = self._window_mid_range[_tok_key][1]
            _wmax = self._window_mid_range[_tok_key][2]
            _BOUNCE_EXTREME = 0.14   # mid was 0.36 or below / 0.64 or above
            _in_entry_range = 0.46 <= _mid_now <= 0.54
            _was_low  = _wmin < 0.50 - _BOUNCE_EXTREME  # market priced DOWN hard
            _was_high = _wmax > 0.50 + _BOUNCE_EXTREME  # market priced UP hard
            if _in_entry_range and (_was_low or _was_high):
                _extreme_val = _wmin if _was_low else _wmax
                _direction = "DOWN" if _was_low else "UP"
                logger.debug(
                    f"[BOUNCE-TRAP] {market.question[:40]} — "
                    f"mid was {_extreme_val:.3f} ({_direction} conviction earlier this window), "
                    f"now bounced to {_mid_now:.3f} → SKIP"
                )
                continue

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

            # Multi-window lookahead: boost rel_strength for markets with >3 min remaining
            # (more time = more potential price movement before oracle update)
            if hours_to_close is not None:
                secs_remaining = hours_to_close * 3600
                if secs_remaining > 180:   # > 3 minutes left
                    sig.rel_strength *= 1.2  # 20% boost for time-rich windows
                elif secs_remaining < 60:   # < 1 minute: urgency penalty
                    sig.rel_strength *= 0.8

            # Live mode: block LOW confidence signals only.
            # MEDIUM requires at least 1 confirmation; HIGH requires 3+.
            # LOW means zero confirmations — pure noise, not worth real capital.
            if not config.DRY_RUN and sig.confidence == "LOW":
                logger.info(
                    f"[LIVE-FILTER] Skipping {_asset} {sig.side} [LOW] — "
                    f"live mode requires MEDIUM or HIGH confidence  \"{sig.question[:40]}\""
                )
                continue

            signals_found += 1
            self._dash_state.push_signal(sig)
            pending_signals.append((sig, _asset))

        # Sort by relative strength (|window_return| / threshold) descending.
        # A signal that exceeds its own threshold by 2× beats one that barely
        # exceeds a lower threshold — ensures per-asset calibration is respected.
        pending_signals.sort(key=lambda x: x[0].rel_strength, reverse=True)

        # No window-level cooldown — per-asset 300s cooldown (COOLDOWN_SECS) and
        # signal quality gates (ENTRY_PRICE_GUARD, thresholds) are sufficient filters.
        if getattr(config, "PENNY_ONLY", False):
            if pending_signals:
                logger.debug(f"[PENNY-ONLY] Skipping {len(pending_signals)} oracle-lag signals")
        elif not getattr(config, "CHAINLINK_ONLY", False):
            for sig, _asset in pending_signals:
                if self._execute_signal(sig):
                    trades_placed += 1
                    self._asset_last_bet[_asset] = time.time()
        else:
            if pending_signals:
                logger.debug(
                    f"[CHAINLINK-ONLY] Skipping {len(pending_signals)} oracle-lag signals"
                )

        # ── BTC penny bets ($0.01 tokens) — HIGHEST PRIORITY ─────────────────
        # Pass the raw unfiltered updown list so near-close markets (< 90s left)
        # are included — that's exactly when one side drops to ≤$0.01.
        self._scan_btc_penny_bets(updown_5m, updown_raw=updown)
        self._start_penny_watcher()  # no-op after first call

        if getattr(config, "PENNY_ONLY", False):
            # All other strategies disabled — penny bets only.
            pass
        else:
            # ── Chainlink close-watch launcher ────────────────────────────────
            self._launch_chainlink_watches(updown)

            # ── Dual-side arbitrage scan ──────────────────────────────────────
            self._scan_dual_arb(updown_5m)

            # ── Claude AI/Momentum news analyst ──────────────────────────────
            self._run_claude_news(updown_5m)

            # ── Market Making (20% bucket) ────────────────────────────────────
            self.run_market_maker(updown_5m)

            # ── Correlation / logical arbitrage ───────────────────────────────
            self._run_corr_arb(all_markets)

            # ── AP/Reuters/BBC News Arbitrage ─────────────────────────────────
            self._run_news_arb()


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


    def _run_corr_arb(self, all_markets: list) -> None:
        """Execute correlation/logical arbitrage signals from corr_arb.scanner."""
        try:
            from src.corr_arb import scanner as _corr
        except Exception:
            return

        _wallet   = self._dash_state.wallet_balance or 0.0
        _arb_pct  = float(getattr(config, "ARB_BUDGET_PCT", 0.30))
        _arb_exp  = sum(
            p.cost_usdc for p in self._positions.values()
            if getattr(p, "strategy", "") == "arb"
        )
        if _arb_exp >= _wallet * _arb_pct:
            return

        signals = _corr.scan(all_markets)
        for sig in signals:
            if sig.token_id in self._positions:
                continue
            if config.TRADING_PAUSED:
                break

            _floor = float(getattr(config, "CAPITAL_FLOOR_USDC", 15.0))
            if _wallet > 0 and _wallet < _floor:
                break

            ob = self._client.get_order_book(sig.token_id)
            if not ob or ob.best_ask <= 0:
                continue
            entry = round(min(ob.best_ask + 0.01, 0.80), 2)

            import math as _m
            usdc   = min(config.MAX_POSITION_USDC, (_wallet * _arb_pct - _arb_exp))
            shares = _m.floor(usdc / entry)
            if shares < config.MIN_ORDER_SHARES:
                continue

            logger.info(
                f"[CORR-ARB] {'[DRY-RUN] ' if config.DRY_RUN else ''}"
                f"Entering {sig.side} {shares:.0f}@{entry:.3f} edge={sig.edge:.1%} | {sig.reason[:60]}"
            )

            if config.DRY_RUN:
                hrs = (sig.hours_to_close or 24.0)
                self._open_sim_position_raw(
                    token_id=sig.token_id, market_id=sig.market_id,
                    question=sig.question, side=sig.side,
                    entry=entry, shares=shares,
                    confidence="CORR-ARB", hours_to_close=hrs, strategy="arb",
                )
                _arb_exp += shares * entry
                continue

            resp = self._client.place_limit_order(
                token_id=sig.token_id, side="BUY", price=entry, size=shares, fok=True
            )
            if not resp:
                continue

            cost = shares * entry
            from src.bot import OpenPosition
            self._positions[sig.token_id] = OpenPosition(
                market_id=sig.market_id, question=sig.question,
                token_id=sig.token_id, side=sig.side,
                shares=shares, entry_price=entry, cost_usdc=cost,
                entry_time=time.time(), strategy="arb", confidence="CORR-ARB",
                composite_signal=sig.edge,
            )
            self._save_positions()
            self._risk.record_open(cost_usdc=cost)
            self._learner.record_open(
                market_id=sig.market_id, token_id=sig.token_id,
                side=sig.side, question=sig.question,
                entry_price=entry, shares=shares, cost_usdc=cost,
                momentum_signal=0.0, imbalance_signal=0.0,
                composite_signal=sig.edge, confidence="CORR-ARB",
                strategy="arb",
            )
            _arb_exp += cost

    def _run_claude_news(self, updown_5m: list) -> None:
        """
        Trigger the Claude AI/Momentum news analyst and execute any emitted signals.
        Reuses the existing _execute_signal path — signals are tagged strategy='momentum'.
        """
        try:
            from src.claude_news import analyst as _claude
        except Exception:
            return
        if not _claude.available:
            return

        # Build asset→YES_mid map from live markets
        _prices: dict[str, float] = {}
        for m in updown_5m:
            from src.strategy import _detect_updown_market
            sym = _detect_updown_market(m.question)
            if sym and 0.01 < m.yes_price < 0.99:
                _prices[sym] = m.yes_price

        _claude.maybe_run(_prices)

        # Consume any signals that came back from the previous cycle
        ai_signals = _claude.pop_signals()
        for ai_sig in ai_signals:
            # Find the matching market and build a TradingSignal-compatible object
            _target_market = None
            for m in updown_5m:
                from src.strategy import _detect_updown_market
                if _detect_updown_market(m.question) == ai_sig.asset:
                    _target_market = m
                    break
            if _target_market is None:
                continue

            tok = (
                _target_market.yes_token.token_id if ai_sig.direction == "YES"
                else _target_market.no_token.token_id
            )
            mkt_mid = ai_sig.market_price if ai_sig.direction == "YES" else 1.0 - ai_sig.market_price

            from src.strategy import TradeSignal, _updown_window_mins, _market_seconds_into_window
            sig = TradeSignal(
                token_id       = tok,
                market_id      = getattr(_target_market, "market_id", "") or getattr(_target_market, "id", "") or "",
                question       = _target_market.question,
                side           = ai_sig.direction,
                fair_value     = ai_sig.ai_probability if ai_sig.direction == "YES" else 1.0 - ai_sig.ai_probability,
                market_price   = mkt_mid,
                edge           = ai_sig.edge,
                confidence     = "AI",
                momentum_signal= 0.0,
                imbalance_signal= 0.0,
                signal         = ai_sig.edge,
                rel_strength   = ai_sig.edge * 100,
                secs_into_window= _market_seconds_into_window(_target_market) or 0,
                win_mins       = _updown_window_mins(_target_market.question) or 5,
                hours_to_close = None,
                is_latency_arb = False,
                best_ask       = None,
                best_bid       = None,
                no_best_ask    = None,
            )
            logger.info(
                f"[CLAUDE-NEWS] AI signal: {ai_sig.asset} {ai_sig.direction} "
                f"ai_prob={ai_sig.ai_probability:.2f} market={ai_sig.market_price:.2f} "
                f"edge={ai_sig.edge:.1%} | {ai_sig.reasoning[:60]}"
            )
            if self._execute_signal(sig):
                self._asset_last_bet[ai_sig.asset] = time.time()

    def _run_news_arb(self) -> None:
        """
        AP/Reuters/BBC News Arbitrage — poll RSS every 15s, match breaking headlines
        to open Polymarket markets, ask Claude to reprice, enter before MMs catch up.
        Also runs price-velocity scanner to detect smart-money moves pre-publication.
        Strategy tag: "news_arb". Requires ANTHROPIC_API_KEY.
        """
        import os as _os
        _api_key = _os.getenv("ANTHROPIC_API_KEY", "")
        if not _api_key:
            return

        # Lazy-init singleton
        if not hasattr(self, "_news_arb"):
            from src.news_arb import NewsArbStrategy
            self._news_arb = NewsArbStrategy(api_key=_api_key, client=self._client)

        self._news_arb.maybe_poll()
        self._news_arb.maybe_check_velocity()  # pre-publication: detect smart-money moves

        for sig in self._news_arb.pop_signals():
            # Budget gate: news_arb gets NEWS_ARB_BUDGET_PCT of wallet
            _budget = getattr(config, "NEWS_ARB_BUDGET_PCT", 0.15)
            _wallet = self._dash_state.wallet_balance or 0.0
            _na_exp = sum(
                p.cost_usdc for p in self._positions.values()
                if getattr(p, "strategy", "") == "news_arb"
            )
            if _wallet > 0 and _na_exp >= _wallet * _budget:
                logger.debug(
                    f"[NEWS-ARB] Budget cap reached — "
                    f"${_na_exp:.2f} >= {_budget:.0%} of ${_wallet:.2f}"
                )
                continue

            # Skip if already in this market
            if sig.token_id in self._positions:
                continue

            # Fetch live order book for best_ask/best_bid
            try:
                ob = self._client.get_order_book(sig.token_id)
            except Exception:
                ob = None

            from src.strategy import TradeSignal
            ts = TradeSignal(
                market_id        = sig.market_id,
                question         = sig.question,
                token_id         = sig.token_id,
                side             = sig.side,
                fair_value       = sig.ai_probability if sig.side == "YES" else 1.0 - sig.ai_probability,
                market_price     = sig.market_price,
                edge             = sig.edge,
                signal           = sig.edge,
                confidence       = "NEWS_ARB",
                is_news_arb      = True,
                hours_to_close   = sig.hours_to_close,
                rel_strength     = sig.edge * 100,
                win_mins         = max(1, int(sig.hours_to_close * 60)),
                best_ask         = ob.best_ask if ob else None,
                best_bid         = ob.best_bid if ob else None,
                momentum_signal  = 0.0,
                imbalance_signal = 0.0,
            )
            logger.info(
                f"[NEWS-ARB] Executing: {sig.side} '{sig.question[:55]}' "
                f"edge={sig.edge:.1%} src={sig.source} | {sig.reasoning[:60]}"
            )
            self._execute_signal(ts)

    def _scan_dual_arb(self, updown_5m: list) -> None:
        """
        Dual-side arbitrage: buy BOTH YES and NO tokens when combined ask < ARB_MAX_COST.

        YES + NO always resolves to exactly $1.00. If we buy both for $0.94 total,
        we earn $0.06 = 6.4% guaranteed regardless of direction.

        Allocation: ARB_BUDGET_PCT of wallet (default 30%). Tracks as strategy="arb".
        """
        _arb_max  = float(getattr(config, "ARB_MAX_COST", 0.97))
        _arb_pct  = float(getattr(config, "ARB_BUDGET_PCT", 0.30))
        _wallet   = self._dash_state.wallet_balance or 0.0

        # Cap arb exposure: count existing arb positions
        _arb_exposure = sum(
            p.cost_usdc for p in self._positions.values()
            if getattr(p, "strategy", "") == "arb"
        )
        _arb_budget = _wallet * _arb_pct
        if _arb_exposure >= _arb_budget:
            return  # arb bucket full

        for market in updown_5m:
            if not self._running:
                break

            # Skip if we already have a position in this market
            yes_tid = market.yes_token.token_id
            no_tid  = market.no_token.token_id
            if yes_tid in self._positions or no_tid in self._positions:
                continue

            # Capital floor check
            _floor = float(getattr(config, "CAPITAL_FLOOR_USDC", 15.0))
            if _wallet > 0 and _wallet < _floor:
                break

            yes_ob = self._client.get_order_book(yes_tid)
            no_ob  = self._client.get_order_book(no_tid)
            if not yes_ob or not no_ob:
                continue

            yes_ask = yes_ob.best_ask
            no_ask  = no_ob.best_ask
            if yes_ask <= 0 or no_ask <= 0:
                continue

            total_cost = yes_ask + no_ask
            if total_cost >= _arb_max:
                continue

            profit_pct = (1.0 - total_cost) / total_cost
            # Only trade if profit margin > 3% (covers 2% Poly fee + gas slop)
            if profit_pct < 0.03:
                continue

            # Size: equal split across both legs, respecting arb budget
            # Don't cap at MAX_POSITION_USDC — arb has its own budget (ARB_BUDGET_PCT).
            # Ensure each leg can buy at least MIN_ORDER_SHARES (5 shares).
            import math as _math
            avg_price    = total_cost / 2
            min_leg_usdc = _math.ceil(config.MIN_ORDER_SHARES * avg_price * 1.05 * 100) / 100
            remaining_budget = _arb_budget - _arb_exposure
            usdc_per_leg = max(min_leg_usdc, min(remaining_budget / 2, 10.0))
            shares = _math.floor(usdc_per_leg / avg_price)
            if shares < config.MIN_ORDER_SHARES:
                continue

            logger.info(
                f"[DUAL-ARB] YES={yes_ask:.3f} + NO={no_ask:.3f} = {total_cost:.3f} "
                f"→ {profit_pct:.1%} guaranteed  {market.question[:45]}"
            )

            if config.DRY_RUN:
                logger.info(
                    f"[DUAL-ARB DRY-RUN] Simulating {shares} YES@{yes_ask:.3f} + "
                    f"{shares} NO@{no_ask:.3f}  {market.question[:40]}"
                )
                mid_id = getattr(market, "market_id", "") or getattr(market, "id", "") or ""
                hrs = 5 / 60  # 5-min window
                self._open_sim_position_raw(
                    token_id=yes_tid, market_id=mid_id, question=market.question,
                    side="YES", entry=yes_ask, shares=shares,
                    confidence="DUAL-ARB", hours_to_close=hrs, strategy="arb",
                )
                self._open_sim_position_raw(
                    token_id=no_tid, market_id=mid_id, question=market.question,
                    side="NO", entry=no_ask, shares=shares,
                    confidence="DUAL-ARB", hours_to_close=hrs, strategy="arb",
                )
                _arb_exposure += (yes_ask + no_ask) * shares
                continue

            # Place YES leg (FOK — guaranteed fill at this price or skip)
            resp_yes = self._client.place_limit_order(
                token_id=yes_tid, side="BUY", price=yes_ask, size=shares, fok=True
            )
            if not resp_yes:
                logger.debug(f"[DUAL-ARB] YES leg rejected — skip {market.question[:40]}")
                continue

            # Place NO leg immediately after
            resp_no = self._client.place_limit_order(
                token_id=no_tid, side="BUY", price=no_ask, size=shares, fok=True
            )
            if not resp_no:
                logger.warning(
                    f"[DUAL-ARB] YES filled but NO rejected — directional YES exposure! "
                    f"{market.question[:40]}"
                )
                # Still track the YES we bought — monitoring will manage it

            mid = getattr(market, "market_id", "") or getattr(market, "id", "") or ""
            from src.bot import OpenPosition
            cost_yes = shares * yes_ask
            cost_no  = shares * no_ask if resp_no else 0.0

            if resp_yes:
                self._positions[yes_tid] = OpenPosition(
                    market_id=mid, question=market.question, token_id=yes_tid,
                    side="YES", shares=shares, entry_price=yes_ask,
                    cost_usdc=cost_yes, entry_time=time.time(), strategy="arb",
                )
            if resp_no:
                self._positions[no_tid] = OpenPosition(
                    market_id=mid, question=market.question, token_id=no_tid,
                    side="NO", shares=shares, entry_price=no_ask,
                    cost_usdc=cost_no, entry_time=time.time(), strategy="arb",
                )

            self._save_positions()
            self._risk.record_open(cost_usdc=cost_yes + cost_no)
            _arb_exposure += cost_yes + cost_no
            logger.info(
                f"[DUAL-ARB] Opened YES+NO {shares}@{yes_ask:.3f}+{no_ask:.3f} "
                f"cost=${cost_yes+cost_no:.2f}  guaranteed {profit_pct:.1%} profit"
            )

    def _launch_chainlink_watches(self, updown_markets: list) -> None:
        """
        For each BTC 5-min UPDOWN market within 5-40s of close, launch a
        Chainlink oracle watch thread. Called every loop — idempotent (ChainlinkMonitor
        prevents double-launching for the same market_id).
        """
        try:
            from src.chainlink import monitor as _cl
        except Exception:
            return
        if not _cl.available:
            return

        from datetime import datetime, timezone as _tz
        from src.strategy import _detect_updown_market, _updown_window_mins
        from src.signals import _price_at_timestamp

        _now = time.time()

        for _m in updown_markets:
            if _detect_updown_market(_m.question) != "BTC":
                continue
            if _updown_window_mins(_m.question) != 5:
                continue
            if not getattr(_m, "end_date", None):
                continue

            try:
                _end_ts = datetime.fromisoformat(
                    _m.end_date.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                continue

            _secs_left = _end_ts - _now
            # Launch as early as 4 minutes before close — the watch thread
            # handles its own POLL_LEAD_SECS sleep. Launching early means we
            # never miss a window due to scan loop timing jitter.
            if not (5 < _secs_left <= 240):
                continue

            # Look up BTC price at window open (300s before close)
            _win_start_ts = _end_ts - 300
            _start_btc = _price_at_timestamp("BTC", _win_start_ts, tolerance_secs=45)

            # Fallback: fetch from Binance 1m kline when history is missing
            # (happens after bot restart — price history doesn't go back far enough)
            if _start_btc is None:
                try:
                    import requests as _req
                    _start_ms = int(_win_start_ts * 1000)
                    _r = _req.get(
                        "https://api.binance.com/api/v3/klines",
                        params={"symbol": "BTCUSDT", "interval": "1m",
                                "startTime": _start_ms, "limit": 1},
                        timeout=3,
                    )
                    if _r.ok:
                        _k = _r.json()
                        if _k:
                            _start_btc = float(_k[0][1])  # open price of that 1m candle
                            logger.debug(
                                f"[CHAINLINK] BTC start price from Binance kline: "
                                f"${_start_btc:,.2f}  {_m.question[:40]}"
                            )
                except Exception as _ke:
                    logger.debug(f"[CHAINLINK] Binance kline fallback failed: {_ke}")

            if _start_btc is None:
                logger.debug(
                    f"[CHAINLINK] No BTC start price for {_m.question[:40]} — skipping"
                )
                continue

            _tok_yes  = _m.yes_token.token_id
            _tok_no   = _m.no_token.token_id
            _mkt_id   = getattr(_m, "market_id", "") or getattr(_m, "id", "") or ""
            _question = _m.question

            # Capture loop vars for the callback closure
            def _make_callback(tok_yes, tok_no, mkt_id, question, start_btc):
                def _on_resolution(direction: str, oracle_price: float) -> None:
                    token_id = tok_yes if direction == "YES" else tok_no
                    self._enter_chainlink_confirmed(
                        token_id=token_id,
                        side=direction,
                        market_id=mkt_id,
                        question=question,
                        oracle_price=oracle_price,
                        start_price=start_btc,
                    )
                return _on_resolution

            _cb = _make_callback(_tok_yes, _tok_no, _mkt_id, _question, _start_btc)
            _cl.watch_async(
                start_price=_start_btc,
                window_close_ts=_end_ts,
                on_resolution=_cb,
                market_id=_mkt_id or _question[:20],
            )
            logger.info(
                f"[CHAINLINK] Watch launched — {_m.question[:45]} | "
                f"{_secs_left:.0f}s to close | start_btc=${_start_btc:,.2f}"
            )

    def _scan_btc_penny_bets(self, updown_5m: list, updown_raw: list | None = None) -> None:
        """
        Buy ANY BTC 5-min UpDown token priced at ≤$0.01, in any direction,
        in any window, at all costs. Max $10 per token. Both YES and NO in
        the same window are bought independently if both are ≤$0.01.
        Bypasses ALL normal rules: cooldowns, exposure caps, trading pause,
        existing positions. Only skips if already holding that exact token.

        updown_raw: unfiltered UpDown market list — includes near-close markets
        (< 90s left) that were dropped from updown_5m but are prime penny targets.
        """
        _MAX_BET   = 5.0
        _PENNY_MAX = 0.01   # ≤1¢ ask price triggers the buy

        from src.bot import OpenPosition

        # Build the full BTC 5-min market list: filtered + near-close raw markets
        # The secs < 90 filter drops markets exactly when prices reach penny levels.
        _btc_all: list = []
        _seen_ids: set = set()
        for _m in updown_5m:
            if _detect_updown_market(_m.question) != "BTC":
                continue
            _mid = getattr(_m, "market_id", "") or getattr(_m, "id", "") or ""
            _seen_ids.add(_mid)
            _btc_all.append(_m)
        # Add near-close BTC markets from raw unfiltered list
        if updown_raw:
            from datetime import datetime, timezone as _tz
            _now_ts = datetime.now(_tz.utc)
            for _m in updown_raw:
                if _detect_updown_market(_m.question) != "BTC":
                    continue
                if _updown_window_mins(_m.question) != 5:
                    continue
                _mid = getattr(_m, "market_id", "") or getattr(_m, "id", "") or ""
                if _mid in _seen_ids:
                    continue  # already included from filtered list
                if not getattr(_m, "active", True) or getattr(_m, "closed", False):
                    continue
                # Only include markets still within their window (not yet closed)
                if _m.end_date:
                    try:
                        _end = datetime.fromisoformat(_m.end_date.replace("Z", "+00:00"))
                        _secs = (_end - _now_ts).total_seconds()
                        if 0 < _secs <= 300:  # must still be open
                            _seen_ids.add(_mid)
                            _btc_all.append(_m)
                    except Exception:
                        pass

        # Rebuild the fast-watcher cache with all BTC token IDs (including near-close)
        # 6-tuple: (side, token_id, opp_token_id, mkt_id, question, end_date)
        _new_cache = []
        for _m in _btc_all:
            _mkt_id_c   = getattr(_m, "market_id", "") or getattr(_m, "id", "") or ""
            _end_date_c = getattr(_m, "end_date", "") or ""
            _yes_id = _m.yes_token.token_id
            _no_id  = _m.no_token.token_id
            _new_cache.append(("YES", _yes_id, _no_id,  _mkt_id_c, _m.question, _end_date_c))
            _new_cache.append(("NO",  _no_id,  _yes_id, _mkt_id_c, _m.question, _end_date_c))
        if hasattr(self, "_btc_penny_token_cache"):
            self._btc_penny_token_cache = _new_cache

        for _m in _btc_all:
            _mkt_id   = getattr(_m, "market_id", "") or getattr(_m, "id", "") or ""
            _question = _m.question

            for (_side, _token_id, _opp_token_id) in [
                ("YES", _m.yes_token.token_id, _m.no_token.token_id),
                ("NO",  _m.no_token.token_id,  _m.yes_token.token_id),
            ]:
                if _token_id in self._positions:
                    continue

                _ob  = self._client.get_order_book(_token_id)
                _ask = (_ob.best_ask if _ob else None)
                if _ask is None or _ask > _PENNY_MAX:
                    continue

                _ask    = max(_ask, 0.001)
                _shares = int(_MAX_BET / _ask)
                if _shares < config.MIN_ORDER_SHARES:
                    continue

                logger.info(
                    f"[BTC-PENNY] {_side} @ ${_ask:.4f} — buying {_shares} shares "
                    f"for ${_MAX_BET:.2f}  {_question[:45]}"
                )
                resp = self._client.place_limit_order(
                    token_id=_token_id, side="BUY",
                    price=round(_ask, 4), size=float(_shares), fok=False,
                )
                if not resp:
                    logger.warning(f"[BTC-PENNY] Order failed for {_side} {_question[:40]}")
                    continue

                _cost    = _shares * _ask
                _sell_ts = time.time() + 30.0
                self._positions[_token_id] = OpenPosition(
                    market_id=_mkt_id, question=_question,
                    token_id=_token_id, side=_side,
                    shares=float(_shares), entry_price=_ask, cost_usdc=_cost,
                    confidence="PENNY", order_id=resp.get("id") if isinstance(resp, dict) else None,
                    entry_time=time.time(), strategy="btc_penny", sell_at_ts=_sell_ts,
                )
                self._save_positions()
                self._risk.record_open(cost_usdc=_cost)
                self._dash_state.orders_placed += 1
                self._learner.record_open(
                    market_id=_mkt_id, token_id=_token_id, side=_side,
                    question=_question, entry_price=_ask, shares=float(_shares),
                    cost_usdc=_cost, confidence="PENNY",
                    dry_run=config.DRY_RUN, strategy="btc_penny",
                )

                # Immediately buy opposing side — hold to resolution
                if _opp_token_id and _opp_token_id not in self._positions:
                    try:
                        _opp_side = "NO" if _side == "YES" else "YES"
                        _opp_ob   = self._client.get_order_book(_opp_token_id)
                        _opp_ask  = _opp_ob.best_ask if _opp_ob else None
                        if _opp_ask and 0 < _opp_ask < 1.0:
                            _opp_shares = max(1, int(_MAX_BET / _opp_ask))
                            _opp_resp = self._client.place_limit_order(
                                token_id=_opp_token_id, side="BUY",
                                price=round(_opp_ask, 4), size=float(_opp_shares), fok=False,
                            )
                            if _opp_resp:
                                _opp_cost = _opp_shares * _opp_ask
                                self._positions[_opp_token_id] = OpenPosition(
                                    market_id=_mkt_id, question=_question,
                                    token_id=_opp_token_id, side=_opp_side,
                                    shares=float(_opp_shares), entry_price=_opp_ask,
                                    cost_usdc=_opp_cost, confidence="PENNY-HEDGE",
                                    entry_time=time.time(), strategy="btc_penny_hedge",
                                )
                                self._save_positions()
                                self._risk.record_open(cost_usdc=_opp_cost)
                                self._dash_state.orders_placed += 1
                                logger.info(
                                    f"[BTC-PENNY-HEDGE] {_opp_side} @ ${_opp_ask:.4f} "
                                    f"— {_opp_shares} shares  {_question[:45]}"
                                )
                    except Exception as _he:
                        logger.debug(f"[BTC-PENNY-HEDGE] hedge failed: {_he}")


def _record_error(msg: str) -> None:
    """Append a runtime error to the error log so the analyst can read it."""
    import json
    from pathlib import Path
    _ERROR_LOG = Path("data/bot_errors.jsonl")
    try:
        _ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ERROR_LOG.open("a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "error": msg}) + "\n")
    except Exception:
        pass
