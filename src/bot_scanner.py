"""
Market scanning and trading hours helpers — mixin for PolymarketBot.
"""
from __future__ import annotations

import time

from loguru import logger

from src.strategy import _detect_updown_market, _updown_window_mins
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
                f"too_soon={n_toosoon} too_late={n_toolate} no_date={n_nodate} expired={n_expired}"
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
