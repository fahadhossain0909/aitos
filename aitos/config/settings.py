"""Centralized configuration. Reads from environment / .env file.

Only Redis is actually wired up by the foundation layer today (it backs the
Event Bus). ClickHouse and Neo4j settings are included now so the Docker
Compose stack and future Data Layer / Knowledge Graph modules can plug in
without a config rewrite.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    # Redis Streams consumers use long-lived blocking connections. The
    # previous 64-connection ceiling was too small once the scanner,
    # lifecycle, journal, learning, and market-data consumers were active.
    # Keep the pool bounded, but leave headroom for blocking readers,
    # publishers, health checks, and short bursts.
    max_connections: int = 256

    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class ClickHouseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLICKHOUSE_")

    host: str = "localhost"
    port: int = 8123
    user: str = "default"
    password: str = ""
    database: str = "aitos"


class Neo4jSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEO4J_")

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "changeme"


class BinanceSettings(BaseSettings):
    """Credentials for LIVE order placement (aitos.execution.binance_executor).

    Not needed for anything else in this codebase — the data layer and
    Opportunity Scanner only use Binance's public endpoints. ``testnet``
    defaults to True as a safety rail; switching to mainnet is an
    explicit, deliberate action, not a config default.
    """

    model_config = SettingsConfigDict(env_prefix="BINANCE_")

    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    recv_window_ms: int = 5000
    hedge_mode: bool = False


class AITOSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = Field(default="dev", description="dev | staging | production")
    # Shared application logging configuration. Keep this at the top level
    # because both paper and live entrypoints consume settings.log_level and
    # the deployment environment supplies LOG_LEVEL.
    log_level: str = Field(default="INFO", description="Python logging level")

    # Governance / safety — production changes require human approval per the AI Constitution.
    require_human_approval_for_prod: bool = True

    redis: RedisSettings = RedisSettings()
    clickhouse: ClickHouseSettings = ClickHouseSettings()
    neo4j: Neo4jSettings = Neo4jSettings()
    binance: BinanceSettings = BinanceSettings()


def get_settings() -> AITOSSettings:
    return AITOSSettings()
