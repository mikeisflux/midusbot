"""
Polymarket client — thin wrapper around the CLOB API (authenticated trading)
and the Gamma API (market data / price history).

Implementation is split across focused mixin modules:
  client_types.py  — shared data-classes (Token, Market, OrderBook, …)
  client_gamma.py  — GammaMixin: public market-data methods
  client_clob.py   — ClobMixin: authenticated trading operations
  client_redeem.py — RedeemMixin: win-redemption + swap-based selling
"""
from __future__ import annotations

import time
from typing import Any

import requests
from loguru import logger

import config
from src.utils import CircuitBreaker

# Re-export types so callers can do `from src.client import Market, OrderBook, …`
from src.client_types import (  # noqa: F401
    Token, Market, OrderBook, PricePoint, Position,
)
from src.client_gamma  import GammaMixin
from src.client_clob   import ClobMixin
from src.client_redeem import RedeemMixin


class PolymarketClient(GammaMixin, ClobMixin, RedeemMixin):
    """
    Unified Polymarket client.

    • Gamma API  — public, no auth — market discovery and price history.
    • CLOB API   — wallet credentials — order placement, positions, order book.
    • Redemption — winning-token claiming via relayer or direct web3.
    """

    _RETRY_DELAYS = (2, 4, 8)

    # Circuit breakers: open after 5 consecutive failures, reset after 60 s
    _cb_gamma = CircuitBreaker("Gamma API", failure_threshold=5, reset_secs=60)
    _cb_clob  = CircuitBreaker("CLOB API",  failure_threshold=5, reset_secs=60)

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        # Bypass any system/env proxy (HTTP_PROXY / HTTPS_PROXY).
        # The bot runs on a server with direct internet access — proxy env vars
        # can be set by tools like Claude Code which use a restricted proxy that
        # blocks Polymarket and Binance. Setting trust_env=False ensures we always
        # connect directly regardless of environment.
        self._session.trust_env = False
        self._clob_client = self._init_clob_client()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_clob_client(self):
        if not config.PRIVATE_KEY:
            logger.warning("PRIVATE_KEY not set — running in data-only mode (no trading)")
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            kwargs = dict(
                host=config.CLOB_HOST,
                chain_id=config.CHAIN_ID,
                key=config.PRIVATE_KEY,
                signature_type=config.SIGNATURE_TYPE,
            )
            if config.FUNDER_ADDRESS:
                kwargs["funder"] = config.FUNDER_ADDRESS
            client = ClobClient(**kwargs)

            if config.CLOB_API_KEY and config.CLOB_API_SECRET and config.CLOB_API_PASSPHRASE:
                creds = ApiCreds(
                    api_key=config.CLOB_API_KEY,
                    api_secret=config.CLOB_API_SECRET,
                    api_passphrase=config.CLOB_API_PASSPHRASE,
                )
            else:
                logger.info("Deriving CLOB API credentials from private key (one-time)…")
                creds = client.create_or_derive_api_creds()
                logger.info(
                    f"Credentials derived — add these to .env to skip re-derivation:\n"
                    f"  CLOB_API_KEY={creds.api_key}\n"
                    f"  CLOB_API_SECRET={creds.api_secret}\n"
                    f"  CLOB_API_PASSPHRASE={creds.api_passphrase}"
                )

            client.set_api_creds(creds)
            logger.info("CLOB client initialised.")
            return client

        except Exception as exc:
            logger.error(f"Failed to initialise CLOB client: {exc}")
            return None

    # ------------------------------------------------------------------
    # Generic HTTP helpers (used by all mixins via self._get)
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> Any:
        cb = self._cb_clob if "clob.polymarket" in url else self._cb_gamma
        if not cb.allow():
            return None
        for attempt, delay in enumerate((*self._RETRY_DELAYS, None), 1):
            try:
                resp = self._session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                cb.success()
                return resp.json()
            except Exception as exc:
                if delay is None:
                    cb.failure()
                    logger.error(f"GET {url} failed after all retries: {exc}")
                    return None
                logger.warning(f"GET {url} attempt {attempt} failed: {exc} — retrying in {delay}s")
                time.sleep(delay)
