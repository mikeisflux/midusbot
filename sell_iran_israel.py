#!/usr/local/bin/python3
"""
One-shot: find "Iran x Israel/US conflict ends by May 15?" YES position and sell it.
Run with:  ./sell_iran_israel.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(override=True)

import config
from src.client import PolymarketClient
from loguru import logger

# Force live mode for this sell regardless of DRY_RUN in .env
config.DRY_RUN = False

client = PolymarketClient()

# ── Step 1: find the position via data-api ────────────────────────────────
address = config.FUNDER_ADDRESS.lower()
print(f"\nFetching positions for {address[:10]}…")
positions = client.get_positions()
print(f"Found {len(positions)} position(s)")

target = None
for p in positions:
    title = (p.get("title") or p.get("question") or "").lower()
    outcome = (p.get("outcome") or "").lower()
    if "iran" in title and ("israel" in title or "conflict" in title) and outcome in ("yes", ""):
        target = p
        print(f"\nMATCH: {p}")
        break

if target is None:
    # Broader search
    for p in positions:
        title = (p.get("title") or p.get("question") or "").lower()
        if "iran" in title:
            print(f"  Iran market found: {p}")
    print("\nNo exact match for 'Iran x Israel' YES position. See matches above.")
    sys.exit(1)

token_id = (
    target.get("asset") or target.get("asset_id") or target.get("assetId") or
    target.get("token_id") or target.get("tokenId") or ""
)
size = float(target.get("size", 0) or 0)

if not token_id or size <= 0:
    print(f"ERROR: Could not extract token_id or size from position: {target}")
    sys.exit(1)

print(f"\nToken ID : {token_id}")
print(f"Shares   : {size:.4f}")

# ── Step 2: get live order book ───────────────────────────────────────────
ob = client.get_order_book(token_id)
if ob is None:
    print("ERROR: Order book returned None — market may be closed/resolved.")
    sys.exit(1)

mid   = ob.mid
bid   = ob.best_bid or mid
ask   = ob.best_ask or mid
print(f"Order book: bid={bid:.4f}  mid={mid:.4f}  ask={ask:.4f}")

if mid >= 0.90:
    print(f"\nPrice {mid:.3f} >= 0.90 — market looks resolved. Attempting redeem instead.")
    market_id = str(target.get("conditionId") or target.get("market_id") or "")
    mkt = client.get_clob_market(market_id) if market_id else None
    neg_risk = bool(mkt.get("neg_risk", False)) if mkt else False
    ok = client.redeem_position(market_id, neg_risk=neg_risk)
    print(f"Redeem result: {ok}")
    sys.exit(0)

# ── Step 3: place SELL limit at best bid ─────────────────────────────────
# Use best_bid minus a small tick to be aggressive and get filled fast.
sell_price = max(round(bid - 0.01, 2), 0.01)
print(f"\nPlacing SELL {size:.4f} shares @ {sell_price:.4f} …")
resp = client.place_limit_order(
    token_id=token_id,
    side="SELL",
    price=sell_price,
    size=size,
)
print(f"\nOrder response: {resp}")
if resp and not resp.get("errorCode") and not resp.get("error"):
    print("SUCCESS — sell order placed.")
else:
    print("FAILED or partial. Check response above.")
