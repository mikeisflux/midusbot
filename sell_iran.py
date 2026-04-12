"""
One-shot: sell the Iran x Israel YES position at market.
Run on VPS: python3 sell_iran.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

# Force live mode so place_limit_order actually fires
import config
config.DRY_RUN = False

from src.client import PolymarketClient

client = PolymarketClient()

print("Fetching positions from Polymarket...")
positions = client.get_positions()
print(f"Found {len(positions)} position(s)")

target = None
for pos in positions:
    if isinstance(pos, dict):
        title = (
            pos.get("title") or
            pos.get("question") or
            (pos.get("market") or {}).get("question", "") or
            str(pos)
        )
    else:
        title = str(pos)
    print(f"  - {str(title)[:80]}")
    if "iran" in str(title).lower() or "israel" in str(title).lower():
        target = pos
        print(f"    ^^^ TARGET")

if not target:
    print("\nCould not find Iran position. Dumping raw data:")
    for pos in positions:
        print(" ", pos)
    sys.exit(1)

# Extract token_id and size
token_id = (
    target.get("asset_id") or
    target.get("token_id") or
    target.get("conditionId") or
    target.get("outcomeTokenId")
)
size = float(
    target.get("size") or
    target.get("shares") or
    target.get("amount") or
    15.0
)

print(f"\nSelling {size} YES shares")
print(f"Token ID: {token_id}")

if not token_id:
    print("ERROR: Could not determine token_id. Position data:")
    print(target)
    sys.exit(1)

# Fetch current order book to find best bid
ob = client.get_order_book(token_id)
if ob and ob.bids:
    best_bid = float(ob.bids[0]["price"]) if isinstance(ob.bids[0], dict) else float(ob.bids[0].price)
    # Sell 2¢ below best bid to guarantee fill
    sell_price = round(max(0.01, best_bid - 0.02), 4)
    print(f"Best bid: {best_bid:.4f} — placing SELL at {sell_price:.4f} (FOK)")
else:
    sell_price = 0.55  # fallback: well below current ~68.5¢ to guarantee fill
    print(f"No order book — placing SELL at {sell_price:.4f} (FOK fallback)")

resp = client.place_limit_order(
    token_id=token_id,
    side="SELL",
    price=sell_price,
    size=size,
    fok=True,
)
print(f"\nResponse: {resp}")
if resp and not resp.get("dry_run"):
    print("SUCCESS — position sold!")
else:
    print("Check response above.")
