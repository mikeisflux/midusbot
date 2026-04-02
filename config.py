import os
from dotenv import load_dotenv

load_dotenv()

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

# ── Position sizing ──────────────────────────────────────────────────────────
MAX_POSITION_USDC: float = float(os.getenv("MAX_POSITION_USDC", "10"))
MAX_TOTAL_EXPOSURE_USDC: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "100"))
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))

# ── Strategy ─────────────────────────────────────────────────────────────────
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.03"))
MIN_ORDER_SHARES: float = float(os.getenv("MIN_ORDER_SHARES", "5.0"))  # Polymarket CLOB minimum
MAX_DAYS_TO_RESOLUTION: int = int(os.getenv("MAX_DAYS_TO_RESOLUTION", "7"))  # skip long-dated markets
MIN_LIQUIDITY_USDC: float = float(os.getenv("MIN_LIQUIDITY_USDC", "1000"))
MIN_VOLUME_24H_USDC: float = float(os.getenv("MIN_VOLUME_24H_USDC", "100"))

# ── Trading costs ────────────────────────────────────────────────────────────
# Polymarket CLOB maker/taker fee (currently 0 %, set > 0 if it changes)
MAKER_FEE_PCT: float = float(os.getenv("MAKER_FEE_PCT", "0.0"))
# Estimated Polygon gas cost per transaction (in USDC)
GAS_COST_USDC: float = float(os.getenv("GAS_COST_USDC", "0.02"))

# ── Loop ─────────────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS: int = int(os.getenv("LOOP_INTERVAL_SECONDS", "60"))
