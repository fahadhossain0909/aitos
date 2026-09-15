#!/usr/bin/env python3
"""AITOS Native Monitor - runs paper trading and sends hourly Telegram updates."""
from __future__ import annotations
import asyncio
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def send_telegram_status():
    """Send hourly Telegram status update."""
    try:
        from aitos.telegram.status_bot import TelegramStatusBot
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            bot = TelegramStatusBot(token, chat_id)
            report = bot.get_status_report()
            bot.send_message(report)
            logger.info("Telegram status update sent")
        else:
            logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
    except Exception as exc:
        logger.error("Telegram update failed: %s", exc)

async def monitor_loop():
    """Main monitoring loop."""
    logger.info("AITOS Monitor started")
    while True:
        try:
            send_telegram_status()
        except Exception as exc:
            logger.error("Monitor error: %s", exc)
        # Wait 1 hour (3600 seconds)
        for _ in range(3600):
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(monitor_loop())
