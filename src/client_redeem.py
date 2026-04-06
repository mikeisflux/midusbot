"""
Redemption & swap mixin — closing won positions for USDC.
Mixed into PolymarketClient.

Two redemption paths:
  1. Gasless relayer via polymarket-apis (preferred — no gas fees, no 100/day cap)
  2. Direct web3 CTF contract call (fallback — requires POLYGON_RPC_URL in .env)
"""
from __future__ import annotations

import time

from loguru import logger

import config


# ConditionalTokens contract on Polygon
_CTF_ADDRESS    = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_USDC_ADDRESS   = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_ZERO_BYTES32   = b"\x00" * 32

# Minimal ABI for redeemPositions + ERC1155 balanceOf
_CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken",     "type": "address"},
            {"name": "parentCollectionId",  "type": "bytes32"},
            {"name": "conditionId",         "type": "bytes32"},
            {"name": "indexSets",           "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Free public Polygon RPC endpoints (tried in order, no auth required)
_POLYGON_RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
]


class RedeemMixin:
    """Mixin providing position redemption and swap-based selling."""

    def _normalize_condition_id(self, condition_id: str) -> str:
        """
        Ensure condition_id is a valid 32-byte hex string for on-chain calls.

        Gamma API often returns numeric IDs like '1869924'; the CLOB and CTF
        contract require a 64-char hex bytes32. If the supplied ID doesn't look
        like bytes32 we try to resolve the real conditionId via the CLOB market
        endpoint before falling back to the original value.
        """
        if not condition_id:
            return condition_id
        _stripped = condition_id.lstrip("0x")
        _is_bytes32 = (
            len(_stripped) == 64
            and all(c in "0123456789abcdefABCDEF" for c in _stripped)
        )
        if _is_bytes32:
            return condition_id  # already valid

        logger.debug(
            f"[REDEEM] condition_id '{condition_id}' is not bytes32 — "
            "looking up real conditionId from CLOB"
        )
        try:
            mkt = self.get_clob_market(condition_id)
            if mkt:
                for key in ("condition_id", "conditionId"):
                    real_cid = mkt.get(key, "")
                    if real_cid and len(real_cid.lstrip("0x")) == 64:
                        logger.info(
                            f"[REDEEM] Resolved '{condition_id}' → "
                            f"{real_cid[:16]}… (real conditionId)"
                        )
                        return real_cid
        except Exception as exc:
            logger.debug(f"[REDEEM] CLOB lookup for condition_id failed: {exc}")

        logger.warning(
            f"[REDEEM] Could not resolve '{condition_id}' to bytes32 — "
            "redeem may fail"
        )
        return condition_id

    def get_ctf_token_balance(self, token_id: str) -> float:
        """
        Query the actual on-chain ERC1155 balance for a Polymarket position token.

        Returns balance in shares (raw uint256 ÷ 1e6).
        Returns -1.0 when the query fails (callers must handle gracefully).
        """
        account = getattr(config, "FUNDER_ADDRESS", "") or ""
        if not account:
            return -1.0

        rpc_urls = []
        for _k in ("POLYGON_RPC_PRIMARY", "POLYGON_RPC_SECONDARY", "POLYGON_RPC_URL"):
            _v = getattr(config, _k, "") or ""
            if _v and _v not in rpc_urls:
                rpc_urls.append(_v)
        rpc_urls.extend([u for u in _POLYGON_RPC_URLS if u not in rpc_urls])

        try:
            from web3 import Web3
            # Polymarket token IDs are decimal integers (not hex).
            # Only parse as hex when the string has an explicit 0x prefix.
            if token_id.lower().startswith("0x"):
                _tok_int = int(token_id, 16)
            else:
                _tok_int = int(token_id)
        except (ValueError, ImportError) as exc:
            logger.debug(f"get_ctf_token_balance: cannot parse token_id '{token_id}': {exc}")
            return -1.0

        for rpc_url in rpc_urls:
            try:
                from web3 import Web3
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
                try:
                    from web3.middleware import geth_poa_middleware
                    w3.middleware_onion.inject(geth_poa_middleware, layer=0)
                except ImportError:
                    try:
                        from web3.middleware import ExtraDataToPOAMiddleware
                        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                    except ImportError:
                        pass
                ctf = w3.eth.contract(
                    address=Web3.to_checksum_address(_CTF_ADDRESS),
                    abi=_CTF_ABI,
                )
                raw_balance = ctf.functions.balanceOf(
                    Web3.to_checksum_address(account),
                    _tok_int,
                ).call()
                balance_shares = raw_balance / 1_000_000  # 6 decimals
                logger.debug(
                    f"[CTF-BAL] {token_id[:12]}… = {balance_shares:.6f} shares "
                    f"(raw={raw_balance}) via {rpc_url}"
                )
                return balance_shares
            except ImportError:
                logger.debug("web3 not installed — cannot check CTF balance")
                return -1.0
            except Exception as exc:
                logger.debug(f"get_ctf_token_balance via {rpc_url}: {exc}")
                continue  # try next RPC

        logger.debug(f"get_ctf_token_balance: all RPCs failed for {token_id[:12]}…")
        return -1.0

    def redeem_position(
        self,
        condition_id: str,
        neg_risk: bool = False,
        amounts: list[float] | None = None,
        outcome_index: int = 0,
        size: float = 0.0,
    ) -> bool:
        """
        Redeem winning tokens for USDC after a market resolves.

        Path 1 (preferred): gasless relayer via polymarket-apis
          — no gas fees, no builder-credit 100 trades/day cap.
        Path 2 (fallback): direct web3 CTF contract call.

        amounts: [yes_amount, no_amount] — if None, built from outcome_index + size,
                 or defaults to [1, 1] (contract pays only the winning side).
        """
        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Would redeem condition {condition_id[:16]}…")
            return True

        # Resolve condition_id to proper bytes32 before any on-chain call
        condition_id = self._normalize_condition_id(condition_id)

        # Build amounts array
        if amounts is None:
            if size > 0:
                amounts = [0.0, 0.0]
                amounts[outcome_index] = size
            else:
                amounts = [1.0, 1.0]

        # ── Path 1: Gasless relayer ────────────────────────────────────────────
        if config.RELAYER_API_KEY and config.RELAYER_API_KEY_ADDRESS:
            try:
                import inspect
                from polymarket_apis.clients.web3_client import PolymarketGaslessWeb3Client
                # Probe actual constructor params — different package versions use
                # different kwarg names (relayer_api_key_address vs api_key_address etc.)
                _sig    = inspect.signature(PolymarketGaslessWeb3Client.__init__)
                _params = set(_sig.parameters.keys())
                _kwargs: dict = {"private_key": config.PRIVATE_KEY}
                # Map our config to whichever param names the installed version uses
                for _rk in ("relayer_api_key", "api_key", "relayer_key"):
                    if _rk in _params:
                        _kwargs[_rk] = config.RELAYER_API_KEY
                        break
                for _ra in ("relayer_api_key_address", "api_key_address", "relayer_address"):
                    if _ra in _params:
                        _kwargs[_ra] = config.RELAYER_API_KEY_ADDRESS
                        break
                if "signature_type" in _params:
                    _kwargs["signature_type"] = config.SIGNATURE_TYPE
                if "funder" in _params:
                    _kwargs["funder"] = config.FUNDER_ADDRESS
                logger.debug(f"[RELAYER] init with params: {list(_kwargs.keys())}")
                web3_client = PolymarketGaslessWeb3Client(**_kwargs)
                for attempt in range(1, 4):
                    try:
                        receipt = web3_client.redeem_position(
                            condition_id=condition_id,
                            amounts=amounts,
                            neg_risk=neg_risk,
                        )
                        logger.info(f"[RELAYER] Redeemed {condition_id[:16]}  receipt={receipt}")
                        return True
                    except Exception as exc:
                        _exc_str = str(exc)
                        # "result for condition not received yet" = market hasn't resolved.
                        # Stop immediately — retrying won't help, and web3 fallback will
                        # also fail. The next monitoring loop will retry when resolved.
                        if ("not received yet" in _exc_str or "result for condition" in _exc_str
                                or "726573756c7420666f7220636f6e646974696f6e" in _exc_str):
                            logger.info(
                                f"[RELAYER] Market not resolved yet — "
                                f"will retry redeem next cycle ({condition_id[:16]}…)"
                            )
                            return False
                        wait = 3 * (2 ** (attempt - 1))
                        if attempt < 3:
                            logger.warning(f"[RELAYER] attempt {attempt}/3 failed: {exc} — retry in {wait}s")
                            time.sleep(wait)
                        else:
                            logger.warning(f"[RELAYER] all 3 attempts failed: {exc} — trying web3 fallback")
            except ImportError as exc:
                logger.warning(
                    f"polymarket-apis ImportError: {exc} — "
                    "install with: pip install polymarket-apis httpx"
                )
            except Exception as exc:
                logger.warning(f"[RELAYER] init failed: {exc} — trying web3 fallback")

        # ── Path 2: Direct web3 CTF contract call ─────────────────────────────
        return self._redeem_via_web3(condition_id)

    def _redeem_via_web3(self, condition_id: str) -> bool:
        """Direct on-chain redemption via web3.py + CTF contract."""
        if not config.PRIVATE_KEY:
            logger.warning("_redeem_via_web3: PRIVATE_KEY not set — cannot redeem")
            return False

        rpc_urls = []
        for _k in ("POLYGON_RPC_PRIMARY", "POLYGON_RPC_SECONDARY", "POLYGON_RPC_URL"):
            _v = getattr(config, _k, "") or ""
            if _v and _v not in rpc_urls:
                rpc_urls.append(_v)
        rpc_urls.extend([u for u in _POLYGON_RPC_URLS if u not in rpc_urls])

        for rpc_url in rpc_urls:
          try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))

            # Polygon is PoA — inject middleware if available (web3 <7) or skip (web3 7+)
            try:
                from web3.middleware import geth_poa_middleware
                w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            except ImportError:
                try:
                    from web3.middleware import ExtraDataToPOAMiddleware
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                except ImportError:
                    pass  # web3 7.x handles PoA natively; no middleware needed

            account = w3.eth.account.from_key(config.PRIVATE_KEY)
            ctf     = w3.eth.contract(
                address=Web3.to_checksum_address(_CTF_ADDRESS),
                abi=_CTF_ABI,
            )

            # condition_id must be bytes32
            cid_bytes = bytes.fromhex(condition_id.lstrip("0x").zfill(64))

            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(_USDC_ADDRESS),
                _ZERO_BYTES32,
                cid_bytes,
                [1, 2],   # index sets for YES (1) and NO (2)
            ).build_transaction({
                "from":  account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas":   200_000,
            })

            signed  = w3.eth.account.sign_transaction(tx, config.PRIVATE_KEY)
            # web3 7.x uses raw_transaction; 6.x used rawTransaction
            raw_tx  = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            logger.info(
                f"[CTF] Redeemed {condition_id[:16]}  "
                f"tx={tx_hash.hex()[:16]}…  status={receipt.status}"
            )
            return receipt.status == 1

          except ImportError:
            logger.warning(
                "web3 not installed — cannot use direct CTF redemption. "
                "Run: pip install web3"
            )
            return False
          except Exception as exc:
            _exc_str = str(exc)
            if ("not received yet" in _exc_str or "result for condition" in _exc_str
                    or "726573756c7420666f7220636f6e646974696f6e" in _exc_str):
                logger.info(
                    f"[CTF] Market not resolved yet via {rpc_url} — "
                    "will retry redeem next cycle"
                )
                return False
            logger.warning(f"_redeem_via_web3 via {rpc_url} failed: {exc}")
            continue  # try next RPC

        logger.warning("_redeem_via_web3: all RPCs exhausted")
        return False

    def sell_via_swaps(
        self,
        token_id: str,
        amount: float,
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> dict | None:
        """
        Sell a position using the swaps.xyz Workflows API.
        Handles order signing, negRisk routing, and tick-size compliance.
        """
        if not config.SWAPS_API_KEY:
            return None
        eoa = config.EVM_EOA
        if not eoa:
            logger.warning("sell_via_swaps: EVM_EOA not configured")
            return None
        if config.DRY_RUN:
            logger.info(f"[DRY-RUN] Would sell {amount:.4f} shares of {token_id[:12]}… via swaps.xyz")
            return {"dry_run": True, "orderResponse": {"success": True}}
        try:
            resp = self._session.post(
                "https://api-v2.swaps.xyz/api/workflows/polymarket/sellPosition",
                headers={"x-api-key": config.SWAPS_API_KEY, "Content-Type": "application/json"},
                json={
                    "evmEoa":     eoa,
                    "side":       "SELL",
                    "tokenID":    token_id,
                    "orderType":  "FAK",
                    "tickSize":   tick_size,
                    "negRisk":    neg_risk,
                    "amount":     amount,
                    "feeRateBps": 0,
                    "slippage":   100,
                },
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"swaps.xyz sell: {result}")
            return result
        except Exception as exc:
            logger.error(f"sell_via_swaps failed: {exc}")
            return None
