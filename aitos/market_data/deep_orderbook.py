"""Deep BTC/LTC order-book journal and deterministic replay primitives.

The live feed records every canonical BOOK_DELTA. Periodic BOOK_SNAPSHOT events
act as checkpoints. Replay starts from the latest checkpoint and applies deltas
in exchange sequence order, failing closed on a gap instead of returning a
silently corrupted book.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .contracts import MarketEvent, MarketEventType

DEEP_SYMBOLS = frozenset({"BTCUSDT", "LTCUSDT"})
DEEP_DELTA_TABLE = "deep_order_book_deltas"
DEEP_CHECKPOINT_TABLE = "deep_order_book_checkpoints"

CREATE_DEEP_ORDERBOOK_TABLES = (
    f"""
    CREATE TABLE IF NOT EXISTS {DEEP_DELTA_TABLE} (
        event_time DateTime64(3, 'UTC'),
        ingest_time DateTime64(3, 'UTC'),
        venue LowCardinality(String),
        market_type LowCardinality(String),
        symbol LowCardinality(String),
        first_update_id UInt64,
        final_update_id UInt64,
        previous_update_id UInt64,
        bids String,
        asks String,
        event_id String
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(event_time)
    ORDER BY (venue, symbol, event_time, final_update_id)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DEEP_CHECKPOINT_TABLE} (
        event_time DateTime64(3, 'UTC'),
        ingest_time DateTime64(3, 'UTC'),
        venue LowCardinality(String),
        market_type LowCardinality(String),
        symbol LowCardinality(String),
        update_id UInt64,
        bids String,
        asks String,
        event_id String
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(event_time)
    ORDER BY (venue, symbol, event_time, update_id)
    """,
)


class DeepOrderBookRepository(Protocol):
    _client: Any


@dataclass(frozen=True, slots=True)
class ReplayLevel:
    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class ReplayBook:
    symbol: str
    update_id: int
    event_time: datetime
    bids: tuple[ReplayLevel, ...]
    asks: tuple[ReplayLevel, ...]


class DeepOrderBookGap(RuntimeError):
    """Raised when a replay segment contains a missing exchange update."""


class DeepOrderBookStore:
    """ClickHouse writer for every raw delta and periodic full checkpoints."""

    def __init__(self, repository: DeepOrderBookRepository) -> None:
        self._repository = repository
        self._initialized = False
        self.deltas_persisted = 0
        self.checkpoints_persisted = 0
        self.rejected_symbols = 0

    async def initialize(self) -> None:
        if self._initialized:
            return
        client = getattr(self._repository, "_client", None)
        if client is None:
            raise RuntimeError("repository ClickHouse client is not initialized")
        for ddl in CREATE_DEEP_ORDERBOOK_TABLES:
            await client.command(ddl)
        self._initialized = True

    @staticmethod
    def _levels(value: Any) -> list[list[float]]:
        return [[float(p), float(q)] for p, q in value]

    async def persist(self, event: MarketEvent) -> None:
        if event.symbol.upper() not in DEEP_SYMBOLS:
            self.rejected_symbols += 1
            return
        client = getattr(self._repository, "_client", None)
        if client is None:
            raise RuntimeError("repository ClickHouse client is not initialized")
        if event.event_type is MarketEventType.BOOK_DELTA:
            p = event.payload
            await client.insert(
                DEEP_DELTA_TABLE,
                [[
                    event.event_time,
                    event.ingest_time,
                    event.venue or event.exchange,
                    event.market_type or event.market,
                    event.symbol.upper(),
                    int(p["first_update_id"]),
                    int(p["final_update_id"]),
                    int(p.get("previous_update_id", 0)),
                    json.dumps(self._levels(p.get("bids", [])), separators=(",", ":")),
                    json.dumps(self._levels(p.get("asks", [])), separators=(",", ":")),
                    event.event_id,
                ]],
                column_names=[
                    "event_time", "ingest_time", "venue", "market_type", "symbol",
                    "first_update_id", "final_update_id", "previous_update_id",
                    "bids", "asks", "event_id",
                ],
            )
            self.deltas_persisted += 1
        elif event.event_type is MarketEventType.BOOK_SNAPSHOT:
            p = event.payload
            await client.insert(
                DEEP_CHECKPOINT_TABLE,
                [[
                    event.event_time,
                    event.ingest_time,
                    event.venue or event.exchange,
                    event.market_type or event.market,
                    event.symbol.upper(),
                    int(p["last_update_id"]),
                    json.dumps(self._levels(p.get("bids", [])), separators=(",", ":")),
                    json.dumps(self._levels(p.get("asks", [])), separators=(",", ":")),
                    event.event_id,
                ]],
                column_names=[
                    "event_time", "ingest_time", "venue", "market_type", "symbol",
                    "update_id", "bids", "asks", "event_id",
                ],
            )
            self.checkpoints_persisted += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "deltas_persisted": self.deltas_persisted,
            "checkpoints_persisted": self.checkpoints_persisted,
            "rejected_symbols": self.rejected_symbols,
        }


class DeepOrderBookReplayer:
    """Reconstruct an order book at an arbitrary time from checkpoint + deltas."""

    def __init__(self, repository: DeepOrderBookRepository) -> None:
        self._repository = repository

    async def reconstruct(
        self, symbol: str, target_time: datetime, *, venue: str = "binance",
        market_type: str = "usd_m_futures",
    ) -> ReplayBook:
        symbol = symbol.upper()
        if symbol not in DEEP_SYMBOLS:
            raise ValueError(f"deep replay is restricted to {sorted(DEEP_SYMBOLS)}")
        client = getattr(self._repository, "_client", None)
        if client is None:
            raise RuntimeError("repository ClickHouse client is not initialized")
        checkpoint = await client.query(
            f"SELECT event_time, update_id, bids, asks FROM {DEEP_CHECKPOINT_TABLE} "
            "WHERE venue={venue:String} AND market_type={market:String} AND symbol={symbol:String} "
            "AND event_time <= {target:DateTime64(3, 'UTC')} "
            "ORDER BY event_time DESC, update_id DESC LIMIT 1",
            parameters={"venue": venue, "market": market_type, "symbol": symbol, "target": target_time},
        )
        if not checkpoint.result_rows:
            raise DeepOrderBookGap(f"no checkpoint exists before {target_time.isoformat()}")
        checkpoint_time, update_id, bids_json, asks_json = checkpoint.result_rows[0]
        bids = {float(p): float(q) for p, q in json.loads(bids_json) if float(q) > 0}
        asks = {float(p): float(q) for p, q in json.loads(asks_json) if float(q) > 0}
        rows = await client.query(
            f"SELECT event_time, first_update_id, final_update_id, previous_update_id, bids, asks "
            f"FROM {DEEP_DELTA_TABLE} WHERE venue={{venue:String}} AND market_type={{market:String}} "
            f"AND symbol={{symbol:String}} AND event_time >= {{since:DateTime64(3, 'UTC')}} "
            f"AND event_time <= {{target:DateTime64(3, 'UTC')}} ORDER BY event_time, final_update_id",
            parameters={"venue": venue, "market": market_type, "symbol": symbol, "since": checkpoint_time, "target": target_time},
        )
        current = int(update_id)
        for event_time, first_id, final_id, previous_id, delta_bids, delta_asks in rows.result_rows:
            first_id, final_id, previous_id = int(first_id), int(final_id), int(previous_id)
            if final_id <= current:
                continue
            if previous_id != current:
                raise DeepOrderBookGap(
                    f"sequence gap for {symbol}: expected pu={current}, got pu={previous_id}, u={final_id}"
                )
            for price, quantity in json.loads(delta_bids):
                price, quantity = float(price), float(quantity)
                if quantity <= 0:
                    bids.pop(price, None)
                else:
                    bids[price] = quantity
            for price, quantity in json.loads(delta_asks):
                price, quantity = float(price), float(quantity)
                if quantity <= 0:
                    asks.pop(price, None)
                else:
                    asks[price] = quantity
            current = final_id
        return ReplayBook(
            symbol=symbol,
            update_id=current,
            event_time=target_time.astimezone(timezone.utc),
            bids=tuple(ReplayLevel(p, q) for p, q in sorted(bids.items(), reverse=True)),
            asks=tuple(ReplayLevel(p, q) for p, q in sorted(asks.items())),
        )
