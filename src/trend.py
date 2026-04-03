"""
Trend tracker for UpDown crypto markets.

Tracks the current bet direction (UP or DOWN) per asset and updates it
based on resolved outcomes:

  - WIN  → keep same direction (trend is continuing)
  - LOSS → flip direction (trend has reversed)

State is persisted to data/trend_state.json so it survives restarts.
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

DATA_DIR = Path("data")


class TrendTracker:
    """
    Per-asset trend state for UpDown markets.

    Usage:
        direction = tracker.get_direction("BTC")   # "UP" | "DOWN" | None
        tracker.record_result("BTC", "UP", won=True)
    """

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._path = DATA_DIR / "trend_state.json"
        # symbol → {"direction": "UP"|"DOWN", "streak": int, "wins": int, "losses": int}
        self._state: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_direction(self, symbol: str) -> str | None:
        """Returns "UP", "DOWN", or None if no history for this asset."""
        entry = self._state.get(symbol.upper())
        return entry["direction"] if entry else None

    def get_streak(self, symbol: str) -> int:
        """Returns the current consecutive-win streak in the tracked direction."""
        entry = self._state.get(symbol.upper())
        return entry.get("streak", 0) if entry else 0

    def record_result(self, symbol: str, direction_bet: str, won: bool) -> None:
        """
        Update trend state after an UpDown market resolves.

        symbol:        asset ticker, e.g. "BTC"
        direction_bet: "UP" or "DOWN" (which side we held)
        won:           True if exit_price >= 0.95 (our token paid 1.0)
        """
        sym = symbol.upper()
        entry = self._state.get(sym, {
            "direction": direction_bet,
            "streak": 0,
            "wins": 0,
            "losses": 0,
        })

        if won:
            entry["wins"] = entry.get("wins", 0) + 1
            entry["direction"] = direction_bet          # stay the course
            entry["streak"] = entry.get("streak", 0) + 1
        else:
            entry["losses"] = entry.get("losses", 0) + 1
            entry["direction"] = "DOWN" if direction_bet == "UP" else "UP"  # flip
            entry["streak"] = 1  # streak of 1 in new direction

        self._state[sym] = entry
        self._save()

        logger.info(
            f"[TREND] {sym}: bet={direction_bet} won={won} "
            f"→ direction={entry['direction']} streak={entry['streak']} "
            f"({entry.get('wins', 0)}W / {entry.get('losses', 0)}L)"
        )

    def summary(self) -> dict:
        return {k: dict(v) for k, v in self._state.items()}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as exc:
            logger.error(f"[TREND] Failed to save state: {exc}")

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._state = json.load(f)
                logger.info(
                    f"[TREND] Loaded state for {len(self._state)} asset(s): "
                    + ", ".join(
                        f"{sym}={v['direction']}(x{v.get('streak', 0)})"
                        for sym, v in self._state.items()
                    )
                )
            except Exception as exc:
                logger.warning(f"[TREND] Could not load state: {exc}")
