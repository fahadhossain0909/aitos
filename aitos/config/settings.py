"""Centralized configuration. Reads from environment / .env file.

Infrastructure credentials and connection settings live here. Performance and
workload knobs are provided by the validated tuning profile resolver.
"""
from __future__ import annotations

from dotenv import load_dotenv

from aitos.config.paths import get_env_file

_ENV_FILE = get_env_file()
if _ENV_FILE.is_file():
    load_dotenv(_ENV_FILE)

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aitos.config.tuning import TuningSettings, load_tuning_settings


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    # Bounded pool sized for blocking stream readers plus publishers and checks.
    max_connections: int = Field(
        default_factory=lambda: load_tuning_settings().event_bus.connection_pool_size,
        ge=8,
        le=4096,
    )

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
    """Credentials for LIVE order placement.

    Public market-data paths do not require these credentials. Testnet stays
    enabled by default so mainnet use remains a deliberate action.
    """
    model_config = SettingsConfigDict(env_prefix="BINANCE_")

    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    recv_window_ms: int = 5000
    hedge_mode: bool = False


class AITOSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = Field(default="dev", description="dev | staging | production")
    log_level: str = Field(default="INFO", description="Python logging level")
    require_human_approval_for_prod: bool = True
    stale_symbol_blacklist: list[str] = Field(
        default=["TRUTHUSDT", "PDDUSDT"],
        description="Symbols to skip in the position price safety-net REST fallback",
    )

    redis: RedisSettings = Field(default_factory=RedisSettings)
    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    binance: BinanceSettings = Field(default_factory=BinanceSettings)
    tuning: TuningSettings = Field(default_factory=load_tuning_settings)


def get_settings() -> AITOSSettings:
    return AITOSSettings()
