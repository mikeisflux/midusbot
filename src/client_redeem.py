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

# Minimal ABI for redeemPositions(collateralToken, parentCollectionId, conditionId, indexSets)
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
    }
]


class RedeemMixin:
    """Mixin providing position redemption and swap-based selling."""

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
                from polymarket_apis.clients.web3_client import PolymarketGaslessWeb3Client
                web3_client = PolymarketGaslessWeb3Client(
                    private_key=config.PRIVATE_KEY,
                    relayer_api_key=config.RELAYER_API_KEY,
                    relayer_api_key_address=config.RELAYER_API_KEY_ADDRESS,
                    signature_type=config.SIGNATURE_TYPE,
                    funder=config.FUNDER_ADDRESS,
                )
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
        rpc_url = getattr(config, "POLYGON_RPC_URL", "") or ""
        if not rpc_url:
            rpc_url = "https://polygon-rpc.com"  # public fallback

        if not config.PRIVATE_KEY:
            logger.warning("_redeem_via_web3: PRIVATE_KEY not set — cannot redeem")
            return False

        try:
            from web3 import Web3
            from web3.middleware import geth_poa_middleware

            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)

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

            signed = w3.eth.account.sign_transaction(tx, config.PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
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
        except Exception as exc:
            logger.warning(f"_redeem_via_web3 failed: {exc}")
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
