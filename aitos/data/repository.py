"""ClickHouse repository for market and long-lived learning data."""

from __future__ import annotations

import json
import time
from typing import Any

import clickhouse_connect

from aitos.core.contracts import AITOSModule, Event, EventResponse, HealthStatus, ModuleStatus
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.learning.experience import ExperienceRecord
from aitos.logging_setup import get_logger
from aitos.models.market import FundingRate, Kline, OpenInterest, OrderBookSnapshot, TradeTick

logger = get_logger("aitos.data.repository")

CREATE_MARKET_OHLCV = """
CREATE TABLE IF NOT EXISTS market_ohlcv (
    time DateTime64(3, 'UTC'), symbol String, timeframe String,
    open Float64, high Float64, low Float64, close Float64,
    volume Float64, quote_volume Float64, trades_count UInt32,
    taker_buy_volume Float64, taker_buy_quote_volume Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, timeframe, time)
"""
CREATE_ORDER_BOOK_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS order_book_snapshots (
    time DateTime64(3, 'UTC'), symbol String, bid_levels String, ask_levels String,
    spread Float64, depth_ratio Float64, last_update_id UInt64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time)
"""
CREATE_TRADE_TICKS = """
CREATE TABLE IF NOT EXISTS trade_ticks (
    time DateTime64(3, 'UTC'), symbol String, trade_id UInt64, price Float64,
    quantity Float64, side String, is_buyer_maker UInt8
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time)
"""
CREATE_FUNDING_RATES = """
CREATE TABLE IF NOT EXISTS funding_rates (
    time DateTime64(3, 'UTC'), symbol String, funding_rate Float64, mark_price Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time)
"""
CREATE_OPEN_INTEREST = """
CREATE TABLE IF NOT EXISTS open_interest (
    time DateTime64(3, 'UTC'), symbol String, open_interest Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time)
"""
CREATE_MARKET_EVENTS = """
CREATE TABLE IF NOT EXISTS market_events (
    event_time DateTime64(3, 'UTC'), ingest_time DateTime64(3, 'UTC'),
    event_id String, exchange LowCardinality(String), market LowCardinality(String),
    symbol LowCardinality(String), event_type LowCardinality(String),
    source LowCardinality(String), sequence Nullable(UInt64), schema_version UInt16,
    correlation_id String, trace_id String, payload_json String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_time)
ORDER BY (symbol, event_type, event_time, event_id)
"""
CREATE_LIVE_ANALYTICS_EVENTS = """
CREATE TABLE IF NOT EXISTS live_analytics_events (
    event_time DateTime64(3, 'UTC'), ingest_time DateTime64(3, 'UTC'),
    event_id String, category LowCardinality(String), exchange LowCardinality(String),
    market LowCardinality(String), symbol LowCardinality(String),
    source_module LowCardinality(String), correlation_id String,
    schema_version String, payload_json String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_time)
ORDER BY (category, symbol, event_time, event_id)
"""
CREATE_LEARNING_EXPERIENCES = """
CREATE TABLE IF NOT EXISTS learning_experiences (
    experience_id UUID, timestamp DateTime64(3, 'UTC'), source LowCardinality(String),
    symbol LowCardinality(String), decision LowCardinality(String), outcome Nullable(String),
    reward Float64, confidence Float64, quantity Float64, price Nullable(Float64),
    features_json String, market_state_json String, risk_state_json String,
    strategy_version String, model_version String, metadata_json String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp, experience_id)
"""
CREATE_TRADE_RUNTIME_STATE = """
CREATE TABLE IF NOT EXISTS trade_runtime_state (
    trade_id String, symbol LowCardinality(String), state LowCardinality(String),
    payload_json String, updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY trade_id
"""
CREATE_PORTFOLIO_DRAWDOWN_STATE = """
CREATE TABLE IF NOT EXISTS portfolio_drawdown_state (
    asset LowCardinality(String), time DateTime64(3, 'UTC'), equity_usd Float64,
    peak_equity_usd Float64, drawdown_pct Float64
) ENGINE = ReplacingMergeTree(time) ORDER BY asset
"""

ALL_DDL = [
    CREATE_MARKET_OHLCV,
    CREATE_ORDER_BOOK_SNAPSHOTS,
    CREATE_TRADE_TICKS,
    CREATE_FUNDING_RATES,
    CREATE_OPEN_INTEREST,
    CREATE_MARKET_EVENTS,
    CREATE_LIVE_ANALYTICS_EVENTS,
    CREATE_LEARNING_EXPERIENCES,
    CREATE_TRADE_RUNTIME_STATE,
    CREATE_PORTFOLIO_DRAWDOWN_STATE,
]


class MarketDataRepository(AITOSModule):
    """Persistent data layer used by paper/live and future historical replay."""

    def __init__(self, host: str = "localhost", port: int = 8123, username: str = "default", password: str = "", database: str = "aitos") -> None:
        self._conn_params = dict(host=host, port=port, username=username, password=password, database=database)
        self._client = None
        self._initialized = False
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "market-data-repository"

    @property
    def version(self) -> str:
        return "1.2.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._client = await clickhouse_connect.get_async_client(**self._conn_params)
        for ddl in ALL_DDL:
            await self._client.command(ddl)
        self._initialized = True
        logger.info("MarketDataRepository initialized with live market/analytics and durable trading tables")

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            await self._client.command("SELECT 1")
            status = ModuleStatus.HEALTHY
        except Exception as exc:
            logger.error("repository health check failed: %s", exc)
            status = ModuleStatus.UNHEALTHY
        return HealthStatus(module_id=self.module_id, status=status, latency_ms=(time.monotonic() - start) * 1000, last_event_time=self._last_event_time, details={})

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._client is not None:
            await self._client.close()

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def ensure_learning_experience_schema(self) -> None:
        self._require_initialized()
        await self._client.command(CREATE_LEARNING_EXPERIENCES)

    async def save_market_event(self, event: Any) -> None:
        """Persist the canonical market envelope for every live market event."""
        self._require_initialized()
        await self._client.insert(
            "market_events",
            [[event.event_time, event.ingest_time, event.event_id, event.exchange, event.market, event.symbol, event.event_type.value, event.source.value, event.sequence, event.schema_version, event.correlation_id or event.event_id, event.trace_id or event.event_id, json.dumps(event.payload, sort_keys=True, default=str)]],
            column_names=["event_time", "ingest_time", "event_id", "exchange", "market", "symbol", "event_type", "source", "sequence", "schema_version", "correlation_id", "trace_id", "payload_json"],
        )
        self._last_event_time = event.event_time.isoformat()

    async def save_live_analytics_event(self, *, category: str, symbol: str, payload: dict[str, Any], event_time: Any, ingest_time: Any | None = None, event_id: str | None = None, exchange: str = "unknown", market: str = "unknown", source_module: str = "unknown", correlation_id: str | None = None, schema_version: str = "1.0") -> None:
        """Persist derived live features, decisions, risk and execution records."""
        self._require_initialized()
        ingest_time = ingest_time or event_time
        event_id = event_id or f"{category}:{symbol}:{event_time.isoformat()}"
        await self._client.insert(
            "live_analytics_events",
            [[event_time, ingest_time, event_id, category, exchange, market, symbol, source_module, correlation_id or event_id, schema_version, json.dumps(payload, sort_keys=True, default=str)]],
            column_names=["event_time", "ingest_time", "event_id", "category", "exchange", "market", "symbol", "source_module", "correlation_id", "schema_version", "payload_json"],
        )

    async def save_kline(self, kline: Kline) -> None:
        self._require_initialized()
        await self._client.insert("market_ohlcv", [[kline.open_time, kline.symbol, kline.timeframe, kline.open, kline.high, kline.low, kline.close, kline.volume, kline.quote_volume, kline.trades_count, kline.taker_buy_volume, kline.taker_buy_quote_volume]], column_names=["time", "symbol", "timeframe", "open", "high", "low", "close", "volume", "quote_volume", "trades_count", "taker_buy_volume", "taker_buy_quote_volume"])

    async def save_order_book_snapshot(self, book: OrderBookSnapshot) -> None:
        self._require_initialized()
        await self._client.insert("order_book_snapshots", [[book.timestamp, book.symbol, json.dumps(book.bids), json.dumps(book.asks), book.spread, book.depth_ratio, book.last_update_id]], column_names=["time", "symbol", "bid_levels", "ask_levels", "spread", "depth_ratio", "last_update_id"])

    async def save_orderbook_snapshot(self, book: OrderBookSnapshot) -> None:
        await self.save_order_book_snapshot(book)

    async def save_trade_tick(self, trade: TradeTick) -> None:
        self._require_initialized()
        await self._client.insert("trade_ticks", [[trade.timestamp, trade.symbol, trade.trade_id, trade.price, trade.quantity, trade.side.value, int(trade.is_buyer_maker)]], column_names=["time", "symbol", "trade_id", "price", "quantity", "side", "is_buyer_maker"])

    async def save_funding_rate(self, funding: FundingRate) -> None:
        self._require_initialized()
        await self._client.insert("funding_rates", [[funding.funding_time, funding.symbol, funding.funding_rate, funding.mark_price]], column_names=["time", "symbol", "funding_rate", "mark_price"])

    async def save_open_interest(self, oi: OpenInterest) -> None:
        self._require_initialized()
        await self._client.insert("open_interest", [[oi.timestamp, oi.symbol, oi.open_interest]], column_names=["time", "symbol", "open_interest"])

    async def save_learning_experience(self, record: ExperienceRecord) -> None:
        self._require_initialized()
        await self._client.insert("learning_experiences", [[record.experience_id, record.timestamp, record.source, record.symbol, record.decision, record.outcome, record.reward, record.confidence, record.quantity, record.price, json.dumps(record.features, sort_keys=True, default=str), json.dumps(record.market_state, sort_keys=True, default=str), json.dumps(record.risk_state, sort_keys=True, default=str), record.strategy_version, record.model_version, json.dumps(record.metadata, sort_keys=True, default=str)]], column_names=["experience_id", "timestamp", "source", "symbol", "decision", "outcome", "reward", "confidence", "quantity", "price", "features_json", "market_state_json", "risk_state_json", "strategy_version", "model_version", "metadata_json"])

    async def get_recent_klines(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict[str, Any]]:
        self._require_initialized()
        result = await self._client.query("SELECT * FROM market_ohlcv WHERE symbol = {symbol:String} AND timeframe = {timeframe:String} ORDER BY time DESC LIMIT {limit:UInt32}", parameters={"symbol": symbol, "timeframe": timeframe, "limit": limit})
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    async def _query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self._require_initialized()
        result = await self._client.query(sql, parameters=parameters)
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError("MarketDataRepository.initialize() must be called first")
