"""
Strategy constants — asset detection map, per-asset thresholds and guard rails.

All values here are the hard-coded defaults.  The analyst / learner can
override per-asset thresholds at runtime via analyst_params.json, but they
are always clamped to the floor/ceil ranges defined here.
"""
from __future__ import annotations

# Maps lowercase keywords in the market question to the Binance symbol
_UPDOWN_ASSETS: dict[str, str] = {
    "xrp":          "XRP",
    "ripple":       "XRP",
    "btc":          "BTC",
    "bitcoin":      "BTC",
    "eth":          "ETH",
    "ethereum":     "ETH",
    "sol":          "SOL",
    "solana":       "SOL",
    "doge":         "DOGE",
    "dogecoin":     "DOGE",
    "bnb":          "BNB",
    "hype":         "HYPE",
    "hyperliquid":  "HYPE",
    "avax":         "AVAX",
    "avalanche":    "AVAX",
    "link":         "LINK",
    "chainlink":    "LINK",
    "ada":          "ADA",
    "cardano":      "ADA",
    "ltc":          "LTC",
    "litecoin":     "LTC",
    "dot":          "DOT",
    "polkadot":     "DOT",
    "matic":        "MATIC",
    "pol":          "MATIC",
    "polygon":      "MATIC",
    "sui":          "SUI",
    "pepe":         "PEPE",
    "wif":          "WIF",
    "dogwifhat":    "WIF",
    "trx":          "TRX",
    "tron":         "TRX",
}

# Minimum absolute 60s momentum to act on (0.05% move in 60s)
_MIN_MOMENTUM_PCT: float = 0.0005

# Global fallback floor — only used when no per-asset threshold exists
# and analyst hasn't set a global signal_threshold.
_MIN_WINDOW_RETURN_PCT: float = 0.0008

# Per-asset default thresholds informed by dry-run history.
_DEFAULT_ASSET_THRESHOLDS: dict[str, float] = {
    "BTC":  0.00035,  # 0.035% — stable large-cap, tiny signals
    "ETH":  0.0004,   # 0.04%  — raise floor slightly
    "BNB":  0.0004,   # 0.04%
    "XRP":  0.0008,   # 0.08%  — lost hard on strongest signal
    "SOL":  0.00035,  # 0.035% — ONLY consistent winner; keep accessible
    "DOGE": 0.0010,   # 0.10%  — systematic losses; severe tightening
    "HYPE": 0.0008,   # 0.08%  — insufficient data; conservative
}

# Hard ceiling — analyst can raise thresholds to filter noise but cannot set
# them so high the asset never trades.
_ASSET_THRESHOLD_CEILS: dict[str, float] = {
    "BTC":  0.00070,
    "ETH":  0.00080,
    "BNB":  0.00080,
    "XRP":  0.00160,
    "SOL":  0.00070,
    "DOGE": 0.00200,
    "HYPE": 0.00160,
    "WIF":  0.00240,
    "TRUMP":0.00300,
}

# Hard floor — analyst/LLM can NEVER lower thresholds below these values.
# Low thresholds cause the bot to trade on noise ticks.
_ASSET_THRESHOLD_FLOORS: dict[str, float] = {
    "BTC":  0.00025,
    "ETH":  0.00025,
    "BNB":  0.00035,
    "XRP":  0.00060,
    "SOL":  0.00025,
    "DOGE": 0.00080,
    "HYPE": 0.00060,
    "WIF":  0.00080,
    "TRUMP":0.00100,
}

# Hard ceiling on the GLOBAL signal_threshold (0.15%)
_GLOBAL_THRESHOLD_CEIL: float = 0.0015
