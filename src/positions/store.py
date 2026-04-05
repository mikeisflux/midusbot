"""
File paths and low-level helpers for persisted position data.

  _POSITIONS_FILE      — open positions dict (token_id → OpenPosition)
  _REDEEMED_FILE       — set of already-redeemed token IDs (survive restarts)
  _CLOSED_MARKETS_FILE — set of market IDs already claimed/closed
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

_POSITIONS_FILE      = Path("data/positions.json")
_REDEEMED_FILE       = Path("data/redeemed_tokens.json")
_CLOSED_MARKETS_FILE = Path("data/closed_market_ids.json")


def load_closed_market_ids() -> set[str]:
    try:
        if _CLOSED_MARKETS_FILE.exists():
            return set(json.loads(_CLOSED_MARKETS_FILE.read_text()))
    except Exception:
        pass
    return set()


def save_closed_market_ids(ids: set[str]) -> None:
    try:
        _CLOSED_MARKETS_FILE.write_text(json.dumps(sorted(ids), indent=2))
    except Exception as exc:
        logger.warning(f"Could not save closed_market_ids: {exc}")


def load_redeemed() -> set[str]:
    """Load persisted set of already-redeemed token IDs."""
    try:
        if _REDEEMED_FILE.exists():
            return set(json.loads(_REDEEMED_FILE.read_text()))
    except Exception:
        pass
    return set()


def save_redeemed(redeemed: set[str]) -> None:
    try:
        _REDEEMED_FILE.write_text(json.dumps(sorted(redeemed), indent=2))
    except Exception as exc:
        logger.warning(f"Could not save redeemed_tokens: {exc}")
