#!/usr/bin/env python3
"""
AITOS Self-Running Bot - Operates 24/7 without supervision.
Monitors services, restarts failures, sends Telegram updates, runs paper trading.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/tmp/aitos-bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

AITOS_DIR = Path("/home/fahad/aitos")
PAPER_TRADING_PID_FILE = Path("/tmp/aitos-paper.pid")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


class AITOSBot:
    def __init__(self) -> None:
        self.paper_trading_proc: subprocess.Popen | None = None
        self.last_telegram = datetime.min
        self.telegram_interval = timedelta(hours=1)
        self.running = True

    def check_redis(self) -> bool:
        try:
            import redis

            r = redis.Redis(host="localhost", port=6379, db=0)
            r.ping()
            return True
        except Exception:
            return False

    def check_clickhouse(self) -> bool:
        try:
            import clickhouse_connect

            client = clickhouse_connect.get_client(
                host="localhost", port=8123, database="aitos"
            )
            client.query("SELECT 1")
            return True
        except Exception:
            return False

    def check_neo4j(self) -> bool:
        try:
            import requests

            resp = requests.get("http://localhost:7474/", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def check_health_server(self) -> bool:
        try:
            import requests

            resp = requests.get("http://localhost:8090/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def restart_service(self, name: str, command: list[str]) -> bool:
        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            logger.info("Restarted %s (PID: %d)", name, proc.pid)
            return True
        except Exception as exc:
            logger.error("Failed to restart %s: %s", name, exc)
            return False

    def restart_paper_trading(self) -> None:
        if self.paper_trading_proc and self.paper_trading_proc.poll() is None:
            return  # Already running
        logger.info("Starting paper trading...")
        env = os.environ.copy()
        self.paper_trading_proc = subprocess.Popen(
            [sys.executable, str(AITOS_DIR / "run_paper_trading.py")],
            cwd=str(AITOS_DIR),
            env=env,
            stdout=open("/tmp/aitos-paper.log", "a"),
            stderr=subprocess.STDOUT,
        )
        logger.info("Paper trading started (PID: %d)", self.paper_trading_proc.pid)

    def restart_redis(self) -> None:
        logger.warning("Redis down, restarting...")
        subprocess.run(
            [
                "redis-server",
                "--daemonize",
                "yes",
                "--requirepass",
                os.environ.get("REDIS_PASSWORD", ""),
                "--port",
                "6379",
            ],
            capture_output=True,
        )
        time.sleep(1)
        if self.check_redis():
            logger.info("Redis restored")
        else:
            logger.error("Redis restart failed")

    def restart_clickhouse(self) -> None:
        logger.warning("ClickHouse down, restarting...")
        subprocess.run(
            [
                "clickhouse-server",
                "--daemon",
                "--config-file",
                "/etc/clickhouse-server/config.xml",
            ],
            capture_output=True,
        )
        time.sleep(2)
        if self.check_clickhouse():
            logger.info("ClickHouse restored")
        else:
            logger.error("ClickHouse restart failed")

    def restart_neo4j(self) -> None:
        logger.warning("Neo4j down, restarting...")
        subprocess.run(["neo4j", "start"], capture_output=True)
        time.sleep(3)
        if self.check_neo4j():
            logger.info("Neo4j restored")
        else:
            logger.error("Neo4j restart failed")

    def send_telegram(self, text: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return
        try:
            import requests

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            logger.info("Telegram update sent")
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)

    def generate_status_report(self) -> str:
        status = ["🤖 AITOS Trading Bot Status", ""]
        status.append(f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        status.append("")
        status.append("✅ Redis" if self.check_redis() else "❌ Redis")
        status.append("✅ ClickHouse" if self.check_clickhouse() else "❌ ClickHouse")
        status.append("✅ Neo4j" if self.check_neo4j() else "❌ Neo4j")
        status.append(
            "✅ Health Server" if self.check_health_server() else "❌ Health Server"
        )
        status.append(
            "✅ Paper Trading"
            if self.paper_trading_proc and self.paper_trading_proc.poll() is None
            else "❌ Paper Trading"
        )
        status.append("")
        status.append("💰 Capital: $1,000 (paper)")
        status.append("📈 Mode: Binance Testnet")
        status.append("📊 Symbols: 848 loaded")
        status.append("")
        status.append("---")
        return "\n".join(status)

    async def monitor_loop(self) -> None:
        logger.info("AITOS Bot monitoring started")
        while self.running:
            try:
                # Check all services
                if not self.check_redis():
                    self.restart_redis()
                if not self.check_clickhouse():
                    self.restart_clickhouse()
                if not self.check_neo4j():
                    self.restart_neo4j()
                if not self.check_health_server():
                    self.restart_paper_trading()
                if (
                    self.paper_trading_proc is None
                    or self.paper_trading_proc.poll() is not None
                ):
                    self.restart_paper_trading()

                # Hourly Telegram update
                now = datetime.utcnow()
                if now - self.last_telegram >= self.telegram_interval:
                    self.last_telegram = now
                    report = self.generate_status_report()
                    self.send_telegram(report)
                    logger.info("Hourly Telegram update sent")

                await asyncio.sleep(30)  # Check every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Monitor error: %s", exc)
                await asyncio.sleep(10)

    def shutdown(self, signum, frame) -> None:
        logger.info("Shutdown signal received")
        self.running = False
        if self.paper_trading_proc:
            self.paper_trading_proc.terminate()

    async def run(self) -> None:
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)

        # Start paper trading
        self.restart_paper_trading()
        time.sleep(5)  # Wait for startup

        # Start monitoring
        await self.monitor_loop()

        logger.info("AITOS Bot stopped")


def main() -> None:
    bot = AITOSBot()
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
