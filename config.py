import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Wallet / auth ────────────────────────────────────────────────────────────
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
CLOB_API_KEY: str = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET: str = os.getenv("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE: str = os.getenv("CLOB_API_PASSPHRASE", "")
# Signature type: 0=EOA (MetaMask), 1=POLY_PROXY (Polymarket exported key), 2=GNOSIS_SAFE
# If you got your private key from Polymarket.com Settings → Private Key, use 1
SIGNATURE_TYPE: int = int(os.getenv("SIGNATURE_TYPE", "1"))
# Funder address: your Polymarket wallet address (shown on polymarket.com top right)
FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")

# ── Network ──────────────────────────────────────────────────────────────────
CHAIN_ID: int = int(os.getenv("CHAIN_ID", "137"))
CLOB_HOST: str = "https://clob.polymarket.com"
GAMMA_HOST: str = "https://gamma-api.polymarket.com"

# ── Safety ───────────────────────────────────────────────────────────────────
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"
# Set TRADING_PAUSED=true to stop all new buys without stopping the bot.
# Position management and manual sells via /positions still work normally.
TRADING_PAUSED: bool = os.getenv("TRADING_PAUSED", "false").lower() == "true"

# ── Position sizing ──────────────────────────────────────────────────────────
MAX_POSITION_USDC: float = float(os.getenv("MAX_POSITION_USDC", "10"))
MAX_TOTAL_EXPOSURE_USDC: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "100"))
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))
# Max % of available wallet balance to deploy at once (0.30 = 30%).
# Overrides MAX_TOTAL_EXPOSURE_USDC when wallet balance is known.
MAX_EXPOSURE_PCT: float = float(os.getenv("MAX_EXPOSURE_PCT", "0.30"))

# ── Strategy ─────────────────────────────────────────────────────────────────
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.03"))
MIN_ORDER_SHARES: float = float(os.getenv("MIN_ORDER_SHARES", "5.0"))    # Polymarket CLOB minimum
MAX_DAYS_TO_RESOLUTION: int = int(os.getenv("MAX_DAYS_TO_RESOLUTION", "90"))   # 90 days covers Stanley Cup (75d); excludes only GTA VI / year-end markets
MIN_MINUTES_TO_RESOLUTION: int = int(os.getenv("MIN_MINUTES_TO_RESOLUTION", "5"))  # don't enter near-expired (UpDown uses 1 min override)
MIN_LIQUIDITY_USDC: float = float(os.getenv("MIN_LIQUIDITY_USDC", "500"))   # lower for 5-min markets
MIN_VOLUME_24H_USDC: float = float(os.getenv("MIN_VOLUME_24H_USDC", "50"))  # lower for 5-min markets

# ── Trading costs ────────────────────────────────────────────────────────────
# Polymarket CLOB maker/taker fee (currently 0 %, set > 0 if it changes)
MAKER_FEE_PCT: float = float(os.getenv("MAKER_FEE_PCT", "0.0"))
# Estimated Polygon gas cost per transaction (in USDC)
GAS_COST_USDC: float = float(os.getenv("GAS_COST_USDC", "0.02"))

# ── External APIs ────────────────────────────────────────────────────────────
# The Odds API — free tier: 500 req/month — https://the-odds-api.com
# Used by SportsSpreadArbStrategy to compare Polymarket vs Vegas consensus lines
ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")

# swaps.xyz Workflows API — used to sell positions without managing CLOB signing
# Create an app at https://console.swaps.xyz to get a key
SWAPS_API_KEY: str = os.getenv("SWAPS_API_KEY", "")
# Your EVM EOA address (the raw wallet address, not the proxy)
EVM_EOA: str = os.getenv("EVM_EOA", FUNDER_ADDRESS)

# ── News trading ─────────────────────────────────────────────────────────────
# Max number of news-arb trades per calendar day (to limit exposure to
# headline-driven bets while still capturing the best opportunities).
MAX_NEWS_TRADES_PER_DAY: int = int(os.getenv("MAX_NEWS_TRADES_PER_DAY", "3"))

# ── Loop ─────────────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS: int = int(os.getenv("LOOP_INTERVAL_SECONDS", "15"))  # fast enough for 5-min markets
