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
MAX_EXPOSURE_PCT: float = float(os.getenv("MAX_EXPOSURE_PCT", "0.60"))

# ── Strategy ─────────────────────────────────────────────────────────────────
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.03"))
MIN_ORDER_SHARES: float = float(os.getenv("MIN_ORDER_SHARES", "5.0"))    # Polymarket CLOB minimum
MAX_DAYS_TO_RESOLUTION: int = int(os.getenv("MAX_DAYS_TO_RESOLUTION", "2"))   # 48-hour max — only bet on markets resolving soon
MIN_MINUTES_TO_RESOLUTION: int = int(os.getenv("MIN_MINUTES_TO_RESOLUTION", "5"))  # don't enter near-expired (UpDown uses 1 min override)
MIN_LIQUIDITY_USDC: float = float(os.getenv("MIN_LIQUIDITY_USDC", "500"))   # lower for 5-min markets
MIN_VOLUME_24H_USDC: float = float(os.getenv("MIN_VOLUME_24H_USDC", "50"))  # lower for 5-min markets

# ── Trading costs ────────────────────────────────────────────────────────────
# Polymarket CLOB maker/taker fee (currently 0 %, set > 0 if it changes)
MAKER_FEE_PCT: float = float(os.getenv("MAKER_FEE_PCT", "0.0"))
# Estimated Polygon gas cost per transaction (in USDC)
GAS_COST_USDC: float = float(os.getenv("GAS_COST_USDC", "0.02"))

# ── External APIs ────────────────────────────────────────────────────────────
# swaps.xyz Workflows API — used to sell positions without managing CLOB signing
# Create an app at https://console.swaps.xyz to get a key
SWAPS_API_KEY: str = os.getenv("SWAPS_API_KEY", "")
# Your EVM EOA address (the raw wallet address, not the proxy)
EVM_EOA: str = os.getenv("EVM_EOA", FUNDER_ADDRESS)

# ── Loop ─────────────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS: int = int(os.getenv("LOOP_INTERVAL_SECONDS", "15"))  # fast enough for 5-min markets

# ── Trading hours (local server time, 24-hour) ────────────────────────────────
# Polymarket 5-min UpDown markets run ~8 AM–11 PM US Central.
# Bot sleeps outside this window to avoid burning API quota on empty scans.
TRADING_HOUR_START: int = int(os.getenv("TRADING_HOUR_START", "8"))   # 8 AM CT
TRADING_HOUR_END:   int = int(os.getenv("TRADING_HOUR_END",   "23"))  # 11 PM CT

# ── Alerts ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL: str  = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN:   str  = os.getenv("DISCORD_BOT_TOKEN",   "")
DISCORD_CHANNEL_ID:  str  = os.getenv("DISCORD_CHANNEL_ID",  "")
