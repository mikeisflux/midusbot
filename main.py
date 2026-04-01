#!/usr/bin/env python3
"""
Polymarket Trading Bot
──────────────────────
Usage:
    cp .env.example .env
    # Edit .env with your PRIVATE_KEY and settings
    pip install -r requirements.txt
    python main.py

By default DRY_RUN=true — the bot will scan markets and log signals but will
NOT submit any real orders.  Set DRY_RUN=false in .env to trade with real USDC.
"""
import sys

from loguru import logger

import config
from src.bot import PolymarketBot


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="DEBUG" if config.DRY_RUN else "INFO",
        colorize=True,
    )
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",   # new file each day
        retention="7 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )


if __name__ == "__main__":
    setup_logging()

    if not config.PRIVATE_KEY:
        logger.warning(
            "PRIVATE_KEY is not set. Running in read-only / data-only mode.\n"
            "Copy .env.example → .env and fill in your key to enable trading."
        )

    if config.DRY_RUN:
        logger.info("DRY-RUN mode: orders will be logged but NOT submitted.")
    else:
        logger.warning("LIVE mode: real USDC orders will be placed on Polymarket!")

    import os
    os.makedirs("logs", exist_ok=True)

    bot = PolymarketBot()
    bot.run()
