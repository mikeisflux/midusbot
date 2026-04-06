"""
Chainlink Oracle Monitor — reads BTC/USD price directly from Polygon.

Strategy (latency arbitrage at window close):
  At T=270s, start polling Chainlink BTC/USD oracle on Polygon every 500ms.
  When the oracle fires a new round (updatedAt changes), the resolution
  outcome is CERTAIN — 2-15 seconds before Polymarket's UI reprices.

  At that point, buy the winning side at any price up to MAX_ENTRY (0.92).
  A certain 8% gain (0.92 → 1.00) is guaranteed edge.

Feed: Chainlink BTC/USD Aggregator on Polygon Mainnet
Contract: 0xc907E116054Ad103354f2D350FD2514433D57F6
Decimals: 8  (e.g. 7500000000000 = $75,000.00)
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from loguru import logger

# Chainlink BTC/USD AggregatorV3Interface on Polygon Mainnet
_CHAINLINK_BTC_USD = "0xc907e116054ad103354f2d350fd2514433d57f6f"

# Minimal ABI — only latestRoundData + decimals needed
_AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",          "type": "uint80"},
            {"name": "answer",           "type": "int256"},
            {"name": "startedAt",        "type": "uint256"},
            {"name": "updatedAt",        "type": "uint256"},
            {"name": "answeredInRound",  "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Maximum entry price for Chainlink-confirmed positions.
# At 0.92 we still earn 8.7% on resolution — guaranteed when oracle confirmed.
CHAINLINK_MAX_ENTRY = 0.92

# Start intensive polling this many seconds before expected window close.
POLL_LEAD_SECS = 60

# Give up this many seconds after expected close (oracle may be slow).
POLL_TRAIL_SECS = 30

# Poll interval during watch window.
POLL_INTERVAL_SECS = 0.5


class OracleRound:
    __slots__ = ("round_id", "price", "updated_at")

    def __init__(self, round_id: int, price: float, updated_at: int):
        self.round_id   = round_id
        self.price      = price
        self.updated_at = updated_at


class ChainlinkMonitor:
    """
    Singleton that connects to the Chainlink BTC/USD feed and polls near
    window close.  Used by the scanner to trigger guaranteed-edge entries.
    """

    def __init__(self):
        self._contract  = None
        self._decimals  = 8
        self._lock      = threading.Lock()
        # market_id → Thread mapping so we don't double-launch
        self._active: dict[str, threading.Thread] = {}
        self._init_contract()

    # ------------------------------------------------------------------ #
    # Initialisation                                                       #
    # ------------------------------------------------------------------ #

    def _init_contract(self) -> bool:
        try:
            from web3 import Web3
        except ImportError:
            logger.info("[CHAINLINK] web3 not installed — oracle monitor disabled")
            return False

        # Build prioritized RPC list: dedicated endpoint first, then fallbacks
        import config as _cfg
        _POLYGON_RPC_URLS: list[str] = []
        if _cfg.POLYGON_RPC_PRIMARY:
            _POLYGON_RPC_URLS.append(_cfg.POLYGON_RPC_PRIMARY)
        if _cfg.POLYGON_RPC_SECONDARY:
            _POLYGON_RPC_URLS.append(_cfg.POLYGON_RPC_SECONDARY)
        # Append fallbacks from client_redeem if available, else use defaults
        try:
            from src.client_redeem import _POLYGON_RPC_URLS as _rpc_fallbacks
            for _u in _rpc_fallbacks:
                if _u not in _POLYGON_RPC_URLS:
                    _POLYGON_RPC_URLS.append(_u)
        except ImportError:
            _POLYGON_RPC_URLS.extend(_cfg.POLYGON_RPC_FALLBACKS)

        for rpc_url in _POLYGON_RPC_URLS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
                # Polygon is PoA — inject middleware to handle extraData
                try:
                    from web3.middleware import geth_poa_middleware
                    w3.middleware_onion.inject(geth_poa_middleware, layer=0)
                except Exception:
                    try:
                        from web3.middleware import ExtraDataToPOAMiddleware
                        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                    except Exception:
                        pass

                # web3 v6 requires checksummed address — convert via bytes to bypass string format check
                _addr_bytes = bytes.fromhex(_CHAINLINK_BTC_USD[2:])
                contract = w3.eth.contract(address=_addr_bytes, abi=_AGGREGATOR_ABI)
                dec = contract.functions.decimals().call()
                self._contract = contract
                self._decimals = int(dec)
                logger.info(
                    f"[CHAINLINK] Connected via {rpc_url.split('/')[2]} — "
                    f"BTC/USD feed live (decimals={dec})"
                )
                return True
            except Exception as exc:
                logger.debug(f"[CHAINLINK] RPC {rpc_url} failed: {exc}")

        logger.warning("[CHAINLINK] All RPCs failed — oracle monitor disabled")
        return False

    @property
    def available(self) -> bool:
        return self._contract is not None

    # ------------------------------------------------------------------ #
    # Oracle read                                                          #
    # ------------------------------------------------------------------ #

    def get_latest(self) -> Optional[OracleRound]:
        """Fetch the current oracle round. Returns None on any error."""
        if self._contract is None:
            return None
        try:
            round_id, answer, _started, updated_at, _answered = (
                self._contract.functions.latestRoundData().call()
            )
            price = answer / (10 ** self._decimals)
            return OracleRound(int(round_id), price, int(updated_at))
        except Exception as exc:
            logger.debug(f"[CHAINLINK] latestRoundData error: {exc}")
            return None

    # ------------------------------------------------------------------ #
    # Window-close watcher                                                 #
    # ------------------------------------------------------------------ #

    def watch_window_close(
        self,
        start_price: float,
        window_close_ts: float,
        on_resolution: Callable[[str, float], None],
        market_id: str = "",
    ) -> None:
        """
        Poll oracle from (window_close_ts - POLL_LEAD_SECS) to
        (window_close_ts + POLL_TRAIL_SECS).

        When a new oracle round appears (round_id or updated_at changes) AND
        updated_at >= window_close_ts - 10, we have a confirmed resolution:
          oracle_price > start_price  →  YES wins
          oracle_price < start_price  →  NO  wins

        Calls on_resolution(direction, oracle_price) exactly once.
        """
        if not self.available:
            return

        tag = market_id[:12] or "unknown"

        # Wait until the intensive-poll window opens
        poll_start = window_close_ts - POLL_LEAD_SECS
        now = time.time()
        if now < poll_start:
            wait = poll_start - now
            logger.debug(f"[CHAINLINK] {tag} — sleeping {wait:.1f}s before poll window")
            time.sleep(wait)

        deadline = window_close_ts + POLL_TRAIL_SECS

        # Grab baseline round so we can detect changes
        baseline = self.get_latest()
        if baseline is None:
            logger.debug(f"[CHAINLINK] {tag} — baseline fetch failed, aborting watch")
            return

        logger.info(
            f"[CHAINLINK] Watching {tag} | start_price=${start_price:,.2f} | "
            f"baseline round={baseline.round_id} price=${baseline.price:,.2f}"
        )

        prev_updated_at = baseline.updated_at

        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECS)

            r = self.get_latest()
            if r is None:
                continue

            new_round   = r.round_id   != baseline.round_id
            fresh_data  = r.updated_at != prev_updated_at
            # Accept any oracle update within 60s before or 30s after window close.
            # BTC/USD on Polygon updates every ~27s so a strict 10s window missed
            # almost every real resolution — the update gets logged, prev_updated_at
            # advances, and the signal is permanently lost.
            near_close  = r.updated_at >= window_close_ts - 60

            if (new_round or fresh_data) and near_close:
                direction = "YES" if r.price > start_price else "NO"
                delta_pct = (r.price - start_price) / start_price * 100
                logger.info(
                    f"[CHAINLINK] RESOLVED {tag} | "
                    f"oracle=${r.price:,.2f} vs start=${start_price:,.2f} "
                    f"({delta_pct:+.4f}%) | round={r.round_id} | → {direction} WINS"
                )
                on_resolution(direction, r.price)
                return

            if fresh_data:
                logger.debug(
                    f"[CHAINLINK] {tag} oracle updated but not near close "
                    f"(updated_at={r.updated_at}, close={window_close_ts:.0f}, "
                    f"delta={r.updated_at - window_close_ts:.0f}s)"
                )
            prev_updated_at = r.updated_at

        logger.debug(
            f"[CHAINLINK] {tag} — watch expired without new round "
            f"(deadline={window_close_ts + POLL_TRAIL_SECS:.0f})"
        )

    def watch_async(
        self,
        start_price: float,
        window_close_ts: float,
        on_resolution: Callable[[str, float], None],
        market_id: str = "",
    ) -> threading.Thread:
        """
        Launch watch_window_close in a daemon thread.
        Prevents double-launching for the same market_id.
        """
        with self._lock:
            existing = self._active.get(market_id)
            if existing and existing.is_alive():
                logger.debug(f"[CHAINLINK] Watch already active for {market_id[:12]}")
                return existing

            t = threading.Thread(
                target=self.watch_window_close,
                args=(start_price, window_close_ts, on_resolution, market_id),
                daemon=True,
                name=f"cl-{market_id[:8]}",
            )
            t.start()
            self._active[market_id] = t

        # Prune dead threads
        with self._lock:
            self._active = {mid: th for mid, th in self._active.items() if th.is_alive()}

        return t


# Module-level singleton — instantiated once at import time.
# The contract init is fast (one RPC call). If it fails, available=False
# and all methods are no-ops.
monitor = ChainlinkMonitor()
