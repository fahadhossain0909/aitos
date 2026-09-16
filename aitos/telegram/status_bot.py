"""AITOS Telegram Status Bot - sends hourly updates."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class TelegramStatusBot:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(self, text: str) -> bool:
        try:
            url = f"{self.base_url}/sendMessage"
            resp = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False

    def get_status_report(self) -> str:
        report = ["🤖 AITOS Trading System Status", ""]
        try:
            import redis

            r = redis.Redis(host="localhost", port=6379, db=0)
            r.ping()
            report.append("✅ Redis: Connected")
        except Exception:
            report.append("❌ Redis: Disconnected")
        try:
            import clickhouse_connect

            from aitos.config.settings import get_settings
            ch_settings = get_settings().clickhouse
            client = clickhouse_connect.get_client(
                host=ch_settings.host,
                port=ch_settings.port,
                user=ch_settings.user,
                password=ch_settings.password,
                database=ch_settings.database,
            )
            client.query("SELECT 1")
            report.append("✅ ClickHouse: Connected")
        except Exception:
            report.append("❌ ClickHouse: Disconnected")
        try:
            resp = requests.get("http://localhost:7474/", timeout=5)
            report.append(f"✅ Neo4j: {resp.status_code}")
        except Exception:
            report.append("❌ Neo4j: Disconnected")
        try:
            resp = requests.get("http://localhost:8090/health", timeout=5)
            report.append(f"✅ Health Server: {resp.status_code}")
        except Exception:
            report.append("❌ Health Server: Down")
        report.extend(
            ["", "📊 Paper Trading Active", "💰 Capital: $1,000", "📈 Mode: Testnet"]
        )
        return "\n".join(report)


def send_hourly_status(bot_token: str, chat_id: str) -> None:
    bot = TelegramStatusBot(bot_token, chat_id)
    bot.send_message(bot.get_status_report())
