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
                if not (0.01 <= m.yes_price <= 0.99):
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

        self._learner.maybe_run_analyst_timed()
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
        _ASSETS_TO_FETCH = ("BTC", "XRP", "ETH", "SOL", "DOGE", "BNB", "HYPE")
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
            for sym in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"):
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
        if not getattr(config, "CHAINLINK_ONLY", False):
            for sig, _asset in pending_signals:
                if self._execute_signal(sig):
                    trades_placed += 1
                    self._asset_last_bet[_asset] = time.time()
        else:
            if pending_signals:
                logger.debug(
                    f"[CHAINLINK-ONLY] Skipping {len(pending_signals)} oracle-lag signals "
                    f"— CHAINLINK_ONLY mode active"
                )

        # ── Chainlink close-watch launcher ────────────────────────────────────
        # For BTC UPDOWN markets within 5-40s of close, launch an oracle watch.
        # When the Chainlink feed fires a new round we know the resolution with
        # certainty — enter the winning side at any price up to CHAINLINK_MAX_ENTRY.
        self._launch_chainlink_watches(updown)

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
            if not (5 < _secs_left <= 40):
                continue

            # Look up BTC price at window open (300s before close)
            _win_start_ts = _end_ts - 300
            _start_btc = _price_at_timestamp("BTC", _win_start_ts, tolerance_secs=45)
            if _start_btc is None:
                logger.debug(
                    f"[CHAINLINK] No BTC start price for {_m.question[:40]} — "
                    f"need price from {_win_start_ts:.0f} (window opened {_now - _win_start_ts:.0f}s ago)"
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
