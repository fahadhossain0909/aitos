"""PostgreSQL paper-runtime market-data adapter.

This adapter preserves canonical incoming market records and the recent OHLCV
read path needed by the scanner. It is not an analytical SQL engine; arbitrary
ClickHouse queries and historical research workloads fail explicitly.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from aitos.core.contracts import HealthStatus, ModuleStatus


class UnsupportedStorageCapabilityError(RuntimeError):
    """The selected storage backend does not implement the requested operation."""


class PostgresMarketDataRepository:
    """Transactional append-only market record store for constrained paper use."""

    CAPABILITIES = frozenset(
        {
            "market_event_writes",
            "live_analytics_writes",
            "ohlcv_writes",
            "ohlcv_recent_reads",
            "orderbook_writes",
            "depth_event_writes",
            "trade_tick_writes",
            "funding_writes",
            "open_interest_writes",
            "learning_experience_writes",
        }
    )

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5) -> None:
        if not dsn or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool = None
        self._initialized = False
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "postgres-market-data-repository"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any] | None = None) -> None:
        if self._initialized:
            return
        import asyncpg

        self._pool = await asyncpg.create_pool(
            dsn=self._dsn, min_size=self._min_size, max_size=self._max_size
        )
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS aitos_market_records (
                        record_id BIGSERIAL PRIMARY KEY,
                        record_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        event_time TIMESTAMPTZ NOT NULL,
                        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        payload JSONB NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_aitos_market_recent
                    ON aitos_market_records (record_type, entity_id, event_time DESC)
                    """
                )
        except Exception:
            await self._pool.close()
            self._pool = None
            raise
        self._initialized = True

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Any):
        return None

    async def health_check(self) -> HealthStatus:
        import time

        started = time.monotonic()
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            status = ModuleStatus.HEALTHY
        except Exception:
            status = ModuleStatus.UNHEALTHY
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=(time.monotonic() - started) * 1000,
            last_event_time=self._last_event_time,
            details={"backend": "postgres", "scope": "paper_market_data"},
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._initialized = False

    async def ensure_learning_experience_schema(self) -> None:
        self._require_initialized()

    async def save_market_event(self, event: Any) -> None:
        payload = {
            "event_time": event.event_time,
            "ingest_time": event.ingest_time,
            "event_id": event.event_id,
            "exchange": event.exchange,
            "market": event.market,
            "symbol": event.symbol,
            "event_type": event.event_type.value,
            "source": event.source.value,
            "sequence": event.sequence,
            "schema_version": event.schema_version,
            "correlation_id": event.correlation_id or event.event_id,
            "trace_id": event.trace_id or event.event_id,
            "payload": event.payload,
        }
        await self._append(
            "MARKET_EVENT",
            event.symbol,
            f"market-event:{event.event_id}",
            payload,
            event.event_time,
        )

    async def save_live_analytics_event(
        self,
        *,
        category: str,
        symbol: str,
        payload: dict[str, Any],
        event_time: Any,
        ingest_time: Any | None = None,
        event_id: str | None = None,
        exchange: str = "unknown",
        market: str = "unknown",
        source_module: str = "unknown",
        correlation_id: str | None = None,
        schema_version: str = "1.0",
    ) -> None:
        ingest_time = ingest_time or event_time
        event_id = event_id or f"{category}:{symbol}:{event_time.isoformat()}"
        record = {
            "category": category,
            "symbol": symbol,
            "payload": payload,
            "ingest_time": ingest_time,
            "exchange": exchange,
            "market": market,
            "source_module": source_module,
            "correlation_id": correlation_id or event_id,
            "schema_version": schema_version,
        }
        await self._append(
            "LIVE_ANALYTICS",
            symbol,
            f"live-analytics:{event_id}",
            record,
            event_time,
        )

    async def save_kline(self, kline: Any) -> None:
        payload = {
            "time": kline.open_time,
            "symbol": kline.symbol,
            "timeframe": kline.timeframe,
            "open": kline.open,
            "high": kline.high,
            "low": kline.low,
            "close": kline.close,
            "volume": kline.volume,
            "quote_volume": kline.quote_volume,
            "trades_count": kline.trades_count,
            "taker_buy_volume": kline.taker_buy_volume,
            "taker_buy_quote_volume": kline.taker_buy_quote_volume,
        }
        identity = f"kline:{kline.symbol}:{kline.timeframe}:{self._time_key(kline.open_time)}"
        await self._append(
            "KLINE",
            kline.symbol,
            identity,
            payload,
            kline.open_time,
            replace=True,
        )

    async def save_order_book_snapshot(self, book: Any) -> None:
        await self.save_order_book_snapshots([book])

    async def save_orderbook_snapshot(self, book: Any) -> None:
        await self.save_order_book_snapshot(book)

    async def save_order_book_snapshots(self, books: list[Any]) -> None:
        for book in books:
            payload = {
                "timestamp": book.timestamp,
                "symbol": book.symbol,
                "bids": book.bids,
                "asks": book.asks,
                "spread": book.spread,
                "depth_ratio": book.depth_ratio if math.isfinite(book.depth_ratio) else None,
                "last_update_id": book.last_update_id,
                "depth_event": book.depth_event,
            }
            key = (
                f"orderbook:{book.symbol}:{self._time_key(book.timestamp)}:"
                f"{book.last_update_id}"
            )
            await self._append(
                "ORDER_BOOK_SNAPSHOT", book.symbol, key, payload, book.timestamp
            )
            event = book.depth_event
            if isinstance(event, dict) and event.get("event_time_ms", 0) > 0:
                event_time = datetime.fromtimestamp(
                    int(event["event_time_ms"]) / 1000, tz=timezone.utc
                )
                final_id = int(event.get("final_update_id", book.last_update_id))
                previous_id = int(
                    event.get("previous_update_id", max(0, book.last_update_id - 1))
                )
                await self._append(
                    "DEPTH_EVENT",
                    book.symbol,
                    f"depth:{book.symbol}:{final_id}",
                    {
                        "symbol": book.symbol,
                        "final_update_id": final_id,
                        "previous_update_id": previous_id,
                        "payload": event,
                    },
                    event_time,
                )

    async def save_depth_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            payload = dict(event.get("payload") or {})
            final_id = payload.get("final_update_id", payload.get("u"))
            event_time = event.get("event_time")
            if final_id is None or event_time is None:
                continue
            symbol = str(event["symbol"]).upper()
            previous_id = payload.get(
                "previous_update_id", payload.get("pu", max(0, int(final_id) - 1))
            )
            await self._append(
                "DEPTH_EVENT",
                symbol,
                f"depth:{symbol}:{int(final_id)}",
                {
                    "symbol": symbol,
                    "final_update_id": int(final_id),
                    "previous_update_id": int(previous_id or 0),
                    "payload": payload,
                },
                event_time,
            )

    async def save_trade_tick(self, trade: Any) -> None:
        await self.save_trade_ticks([trade])

    async def save_trade_ticks(self, trades: list[Any]) -> None:
        for trade in trades:
            payload = {
                "time": trade.timestamp,
                "symbol": trade.symbol,
                "trade_id": trade.trade_id,
                "price": trade.price,
                "quantity": trade.quantity,
                "side": trade.side.value,
                "is_buyer_maker": bool(trade.is_buyer_maker),
            }
            await self._append(
                "TRADE_TICK",
                trade.symbol,
                f"trade-tick:{trade.symbol}:{trade.trade_id}",
                payload,
                trade.timestamp,
            )

    async def save_funding_rate(self, funding: Any) -> None:
        payload = {
            "time": funding.funding_time,
            "symbol": funding.symbol,
            "funding_rate": funding.funding_rate,
            "mark_price": funding.mark_price,
        }
        await self._append(
            "FUNDING_RATE",
            funding.symbol,
            f"funding:{funding.symbol}:{self._time_key(funding.funding_time)}",
            payload,
            funding.funding_time,
        )

    async def save_open_interest(self, oi: Any) -> None:
        payload = {
            "time": oi.timestamp,
            "symbol": oi.symbol,
            "open_interest": oi.open_interest,
        }
        await self._append(
            "OPEN_INTEREST",
            oi.symbol,
            f"open-interest:{oi.symbol}:{self._time_key(oi.timestamp)}",
            payload,
            oi.timestamp,
        )

    async def save_learning_experience(self, record: Any) -> None:
        payload = {
            "experience_id": str(record.experience_id),
            "timestamp": record.timestamp,
            "source": record.source,
            "symbol": record.symbol,
            "decision": record.decision,
            "outcome": record.outcome,
            "reward": record.reward,
            "confidence": record.confidence,
            "quantity": record.quantity,
            "price": record.price,
            "features": record.features,
            "market_state": record.market_state,
            "risk_state": record.risk_state,
            "strategy_version": record.strategy_version,
            "model_version": record.model_version,
            "metadata": record.metadata,
        }
        await self._append(
            "LEARNING_EXPERIENCE",
            record.symbol,
            f"experience:{record.experience_id}",
            payload,
            record.timestamp,
        )

    async def get_recent_klines(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        self._require_initialized()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT payload::text AS payload
                FROM aitos_market_records
                WHERE record_type = 'KLINE'
                  AND entity_id = $1
                  AND payload->>'timeframe' = $2
                ORDER BY event_time DESC
                LIMIT $3
                """,
                symbol,
                timeframe,
                max(1, int(limit)),
            )
        return [self._decode_payload(row["payload"]) for row in rows]

    async def _append(
        self,
        record_type: str,
        entity_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        event_time: Any,
        *,
        replace: bool = False,
    ) -> None:
        self._require_initialized()
        occurred_at = self._as_datetime(event_time)
        encoded = json.dumps(
            payload,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
            allow_nan=False,
        )
        conflict_action = (
            "DO UPDATE SET event_time = EXCLUDED.event_time, payload = EXCLUDED.payload"
            if replace
            else "DO NOTHING"
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO aitos_market_records
                    (record_type, entity_id, idempotency_key, event_time, payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (idempotency_key) {conflict_action}
                """,
                record_type,
                entity_id,
                idempotency_key,
                occurred_at,
                encoded,
            )
        self._last_event_time = occurred_at.isoformat()

    @staticmethod
    def _decode_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {"value": decoded}
        return dict(value)

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        raise TypeError("market record event_time must be datetime or ISO timestamp")

    @classmethod
    def _time_key(cls, value: Any) -> str:
        return cls._as_datetime(value).isoformat()

    async def _query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise UnsupportedStorageCapabilityError(
            "arbitrary ClickHouse SQL queries are not supported by the "
            "PostgreSQL paper-runtime adapter"
        )

    def _require_initialized(self) -> None:
        if not self._initialized or self._pool is None:
            raise RuntimeError("PostgresMarketDataRepository is not initialized")
