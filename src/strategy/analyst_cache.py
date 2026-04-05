"""
Analyst params TTL cache — avoids a disk read on every analyse() call.

analyst_params.json is written at most once per adaptation cycle (~5 trades),
so a 5-second cache hits fresh enough values without hammering the filesystem.
"""
from __future__ import annotations

import time

_AP_CACHE: dict = {}
_AP_CACHE_TS: float = 0.0
_AP_CACHE_TTL: float = 5.0   # seconds


def _load_analyst_params_cached() -> dict:
    global _AP_CACHE, _AP_CACHE_TS
    now = time.time()
    if now - _AP_CACHE_TS < _AP_CACHE_TTL and _AP_CACHE_TS > 0:
        return _AP_CACHE
    try:
        from src.analyst import load_params
        _AP_CACHE = load_params()
        _AP_CACHE_TS = now
    except Exception:
        pass
    return _AP_CACHE
