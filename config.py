import os
from dotenv import load_dotenv

load_dotenv(override=True)


def _env(key: str, default: str) -> str:
    """Read env var, treating empty string as missing (uses default instead)."""
    val = os.getenv(key, "")
    return val if val else default

# ── Wallet / auth ────────────────────────────────────────────────────────────
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
CLOB_API_KEY: str = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET: str = os.getenv("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE: str = os.getenv("CLOB_API_PASSPHRASE", "")
# Signature type: 0=EOA (MetaMask), 1=POLY_PROXY (Polymarket exported key), 2=GNOSIS_SAFE
# If you got your private key from Polymarket.com Settings → Private Key, use 1
SIGNATURE_TYPE: int = int(_env("SIGNATURE_TYPE", "1"))
# Funder address: your Polymarket wallet address (shown on polymarket.com top right)
FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")

# ── Network ──────────────────────────────────────────────────────────────────
CHAIN_ID: int = int(_env("CHAIN_ID", "137"))
CLOB_HOST: str = "https://clob.polymarket.com"
GAMMA_HOST: str = "https://gamma-api.polymarket.com"

# ── Safety ───────────────────────────────────────────────────────────────────
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() != "false"
# Set ANALYST_ENABLED=true to run the LLM analyst (disabled by default).
# The analyst reads data/analyst_params.json for thresholds regardless.
ANALYST_ENABLED: bool = os.getenv("ANALYST_ENABLED", "false").lower() == "true"
# Set TRADING_PAUSED=true to stop all new buys without stopping the bot.
# Position management and manual sells via /positions still work normally.
TRADING_PAUSED: bool = os.getenv("TRADING_PAUSED", "false").lower() == "true"
# Hard stop all new trades if wallet drops below this threshold (USDC).
# Prevents trading to zero. Override via env: CAPITAL_FLOOR_USDC=20
CAPITAL_FLOOR_USDC: float = float(_env("CAPITAL_FLOOR_USDC", "15.0"))
# Daily trading floor: halt new trades if live wallet balance drops below
# this value. Resets automatically if wins push balance back above it.
# Override via env: DAILY_LOSS_FLOOR_USDC=80
DAILY_LOSS_FLOOR_USDC: float = float(_env("DAILY_LOSS_FLOOR_USDC", "90.0"))

# ── Position sizing ──────────────────────────────────────────────────────────
MAX_POSITION_USDC: float = float(_env("MAX_POSITION_USDC", "10"))
MAX_TOTAL_EXPOSURE_USDC: float = float(_env("MAX_TOTAL_EXPOSURE_USDC", "100"))
KELLY_FRACTION: float = float(_env("KELLY_FRACTION", "0.25"))
# Max % of available wallet balance to deploy at once (0.30 = 30%).
# Overrides MAX_TOTAL_EXPOSURE_USDC when wallet balance is known.
MAX_EXPOSURE_PCT: float = float(_env("MAX_EXPOSURE_PCT", "0.60"))

# ── Strategy ─────────────────────────────────────────────────────────────────
MIN_EDGE: float = float(_env("MIN_EDGE", "0.03"))
MIN_ORDER_SHARES: float = float(_env("MIN_ORDER_SHARES", "5.0"))    # Polymarket CLOB minimum
MAX_DAYS_TO_RESOLUTION: int = int(_env("MAX_DAYS_TO_RESOLUTION", "2"))   # 48-hour max — only bet on markets resolving soon
MIN_MINUTES_TO_RESOLUTION: int = int(_env("MIN_MINUTES_TO_RESOLUTION", "5"))  # don't enter near-expired (UpDown uses 1 min override)
MIN_LIQUIDITY_USDC: float = float(_env("MIN_LIQUIDITY_USDC", "500"))   # lower for 5-min markets
MIN_VOLUME_24H_USDC: float = float(_env("MIN_VOLUME_24H_USDC", "50"))  # lower for 5-min markets

# ── Trading costs ────────────────────────────────────────────────────────────
# Polymarket CLOB maker/taker fee (currently 0 %, set > 0 if it changes)
MAKER_FEE_PCT: float = float(_env("MAKER_FEE_PCT", "0.0"))
# Estimated Polygon gas cost per transaction (in USDC)
GAS_COST_USDC: float = float(_env("GAS_COST_USDC", "0.02"))

# ── External APIs ────────────────────────────────────────────────────────────
# swaps.xyz Workflows API — used to sell positions without managing CLOB signing
# Create an app at https://console.swaps.xyz to get a key
SWAPS_API_KEY: str = os.getenv("SWAPS_API_KEY", "")
# Your EVM EOA address (the raw wallet address, not the proxy)
EVM_EOA: str = os.getenv("EVM_EOA", FUNDER_ADDRESS)

# ── Gasless relayer (polymarket-claimer approach) ─────────────────────────────
# Allows redeeming winning positions without gas fees and without the
# 100-trades/day builder credit limit of direct contract calls.
# Get a relayer key from Polymarket (same place as CLOB API keys).
RELAYER_API_KEY:         str = os.getenv("RELAYER_API_KEY", "")
RELAYER_API_KEY_ADDRESS: str = os.getenv("RELAYER_API_KEY_ADDRESS", "")

# ── Loop ─────────────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS: int = int(_env("LOOP_INTERVAL_SECONDS", "15"))  # fast enough for 5-min markets

# ── Trading hours (local server time, 24-hour) ────────────────────────────────
# Polymarket 5-min UpDown markets run ~8 AM–11 PM US Central.
# Bot sleeps outside this window to avoid burning API quota on empty scans.
TRADING_HOUR_START: int = int(_env("TRADING_HOUR_START", "0"))   # default: always on
TRADING_HOUR_END:   int = int(_env("TRADING_HOUR_END",   "24"))  # set in .env to restrict hours

# ── Alerts ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL: str  = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN:   str  = os.getenv("DISCORD_BOT_TOKEN",   "")
DISCORD_CHANNEL_ID:  str  = os.getenv("DISCORD_CHANNEL_ID",  "")

# ── Centralized tunable strategy constants ────────────────────────────────────
# Previously scattered across multiple files — consolidated here so tuning
# is a single-file change. Override any via env var.

# Entry guard: skip markets where YES price has moved beyond this (oracle lag gone)
ENTRY_PRICE_GUARD:   float = float(_env("ENTRY_PRICE_GUARD",   "0.54"))  # src/strategy.py

# Early exit thresholds: exit positions at these price extremes mid-window
EARLY_EXIT_LOSS_THRESHOLD:   float = float(_env("EARLY_EXIT_LOSS_THRESHOLD",   "0.38"))
EARLY_EXIT_GAIN_THRESHOLD:   float = float(_env("EARLY_EXIT_GAIN_THRESHOLD",   "0.72"))

# Order book thinness: skip if YES spread < this (MMs repricing aggressively)
OB_MIN_SPREAD:      float = float(_env("OB_MIN_SPREAD", "0.005"))

# Chainlink-only mode: skip all oracle-lag (T=0) entries; enter only via Chainlink
# oracle confirmation at T≈270s. Set CHAINLINK_ONLY=true to focus exclusively on
# the latency-arbitrage strategy at window close.
CHAINLINK_ONLY:     bool  = _env("CHAINLINK_ONLY", "false").lower() == "true"

# Wallet replenishment alert threshold (USDC)
WALLET_REPLENISH_ALERT: float = float(_env("WALLET_REPLENISH_ALERT", "25.0"))

# Correlation cooldown between bets on correlated assets (seconds)
COOLDOWN_SECS:      int = int(_env("COOLDOWN_SECS", "300"))

# Group exposure cap for correlated assets (fraction of wallet)
GROUP_CAP_PCT:      float = float(_env("GROUP_CAP_PCT", "0.25"))

# Bankroll-proportional position sizing (fraction of wallet per trade)
POSITION_WALLET_PCT: float = float(_env("POSITION_WALLET_PCT", "0.12"))

# Polymarket win fee (2% on winnings — adjusts Kelly payout ratio)
POLY_WIN_FEE:       float = float(_env("POLY_WIN_FEE", "0.02"))

