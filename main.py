#!/usr/bin/env python3
"""
Polymarket Trading Bot
──────────────────────
Usage:
    cp .env.example .env
    # Edit .env with your PRIVATE_KEY and settings
    pip install -r requirements.txt
    python main.py                 # full-screen dashboard
    python main.py --no-dashboard  # plain log output

Strategies:
  1. Momentum + Order-Book Imbalance  — scans all active markets
  2. Latency Arbitrage                — exploits crypto contract price lag

The bot self-learns: it tracks every trade, and every 10 closed trades it
adapts signal weights, thresholds, and Kelly sizing based on what actually
worked.  Learned parameters persist in data/learned_params.json.

DRY_RUN=true (default) → analyse only, no real orders.
"""
import argparse
import os
import sys

from loguru import logger

import config
from src.bot import PolymarketBot


def setup_logging(*, dashboard: bool) -> None:
    logger.remove()

    # Always log to file
    os.makedirs("logs", exist_ok=True)
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="7 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )

    # Terminal output only when dashboard is off (dashboard owns the screen)
    if not dashboard:
        logger.add(
            sys.stderr,
            format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
            level="DEBUG" if config.DRY_RUN else "INFO",
            colorize=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the live terminal dashboard; use plain log output.",
    )
    args = parser.parse_args()

    use_dashboard = not args.no_dashboard
    setup_logging(dashboard=use_dashboard)

    if not config.PRIVATE_KEY:
        logger.warning(
            "PRIVATE_KEY not set — running in data-only mode.\n"
            "Copy .env.example → .env and fill in your key to enable trading."
        )

    if config.DRY_RUN:
        logger.info("DRY-RUN mode: orders will be logged but NOT submitted.")
    else:
        logger.warning("LIVE mode: real USDC orders will be placed on Polymarket!")

    os.makedirs("data", exist_ok=True)

    bot = PolymarketBot(dashboard_enabled=use_dashboard)
    bot.run()


if __name__ == "__main__":
    main()
