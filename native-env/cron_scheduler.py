#!/usr/bin/env python3
"""Simple cron-style scheduler for AITOS."""
from __future__ import annotations

import logging
import subprocess
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_task(command: list[str], name: str):
    """Run a background task."""
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        logger.info("Started %s (PID: %d)", name, proc.pid)
        return proc
    except Exception as exc:
        logger.error("Failed to start %s: %s", name, exc)
        return None


if __name__ == "__main__":
    # Start paper trading if not running
    # Start Telegram monitor
    logger.info("AITOS scheduler starting")
    while True:
        time.sleep(60)
