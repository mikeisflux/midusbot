"""
Position persistence — save/load open positions to/from disk.

PersistenceMixin provides:
  _save_positions       — write positions dict to data/positions.json
  _load_positions       — restore positions on startup; rebuilds risk exposure
  _mark_market_closed   — add market_id to closed set and persist
  _already_positioned   — check if bot has an open position for a market
"""
from __future__ import annotations

import json
from dataclasses import asdict

from loguru import logger

from src.positions.store import (
    _POSITIONS_FILE,
    load_closed_market_ids,
    save_closed_market_ids,
)


class PersistenceMixin:

    def _save_positions(self) -> None:
        try:
            _POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_POSITIONS_FILE, "w") as f:
                json.dump({tid: asdict(pos) for tid, pos in self._positions.items()}, f, indent=2)
        except Exception as exc:
            logger.warning(f"_save_positions failed: {exc}")

    def _load_positions(self) -> None:
        if not _POSITIONS_FILE.exists():
            return
        try:
            with open(_POSITIONS_FILE) as f:
                raw = json.load(f)
            from src.bot import OpenPosition
            count = 0
            for token_id, d in raw.items():
                if token_id not in self._positions:
                    fields = {
                        k: v for k, v in d.items()
                        if k in OpenPosition.__dataclass_fields__
                    }
                    if "is_external" not in d:
                        fields["is_external"] = True
                    self._positions[token_id] = OpenPosition(**fields)
                    count += 1
            if count:
                logger.info(f"Restored {count} open position(s) from disk.")
                # Rebuild risk exposure counter so GROUP_CAP is correctly
                # enforced immediately after restart (not just after first trade).
                for pos in self._positions.values():
                    self._risk.record_open(cost_usdc=getattr(pos, "cost_usdc", 0.0))
        except Exception as exc:
            logger.warning(f"_load_positions failed: {exc}")

    def _mark_market_closed(self, market_id: str) -> None:
        """Add market to closed set and persist to disk so restarts don't re-process."""
        if market_id:
            self._closed_market_ids.add(market_id)
            save_closed_market_ids(self._closed_market_ids)

    def _already_positioned(self, market, token_id: str | None = None) -> bool:
        if token_id:
            return token_id in self._positions
        if market.id in self._closed_market_ids:
            return True
        return (
            market.yes_token.token_id in self._positions
            or market.no_token.token_id in self._positions
        )
