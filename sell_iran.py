"""
One-shot: sell the Iran x Israel YES position at market.
Run on VPS: python sell_iran.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(override=True)

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON

import config

client = ClobClient(
    host=config.CLOB_HOST,
    chain_id=POLYGON,
    key=config.PRIVATE_KEY,
    signature_type=config.SIGNATURE_TYPE,
    funder=config.FUNDER_ADDRESS,
    creds={
        "apiKey":      config.CLOB_API_KEY,
        "secret":      config.CLOB_API_SECRET,
        "passphrase":  config.CLOB_API_PASSPHRASE,
    },
)
client.set_api_creds(client.create_or_derive_api_creds())

# Find the Iran market
print("Searching for Iran position...")
positions = client.get_positions()
target = None
for pos in positions:
    q = getattr(pos, "title", "") or getattr(pos, "question", "") or str(pos)
    if "iran" in q.lower() or "israel" in q.lower():
        print(f"  Found: {q}")
        target = pos
        break

if not target:
    # Try alternate field names
    for pos in positions:
        pd = pos if isinstance(pos, dict) else vars(pos)
        print("  Pos:", pd)
    print("Could not find Iran position — check output above")
    sys.exit(1)

token_id = getattr(target, "token_id", None) or getattr(target, "asset_id", None)
size = float(getattr(target, "size", 0) or getattr(target, "shares", 0) or 15.0)

print(f"Selling {size} YES shares (token={token_id})...")

# Market sell — crosses the spread to guarantee fill
order = client.create_market_order(
    MarketOrderArgs(
        token_id=token_id,
        amount=size,
        side="SELL",
    )
)
resp = client.post_order(order, OrderType.FOK)
print("Response:", resp)
