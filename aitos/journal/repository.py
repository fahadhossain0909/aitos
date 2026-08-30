"""ClickHouse repository for the Journal System — spec §7.2's
``journal_entries`` table (adapted to ClickHouse), plus a ``trades`` log.

Trades are append-only snapshots here (one row per state change — opened,
then closed) rather than updated in place, since ClickHouse's MergeTree
favors inserts; querying the latest row per ``trade_id`` gives current
state, and the full history is preserved as a side effect (itself useful
for audit/journal purposes).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.journal.models import JournalEntry
from aitos.logging_setup import get_logger

logger = get_logger("aitos.journal.repository")

CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    recorded_at DateTime64(3, 'UTC'),
    trade_id String,
    symbol String,
    side String,
    entry_price Float64,
    quantity Float64,
    leverage Float64,
    position_size_usd Float64,
    risk_amount_usd Float64,
    strategy_id String,
    sl_price Float64,
    tp_price Float64,
    state String,
    entry_time String,
    exit_price Nullable(Float64),
    exit_time Nullable(String),
    exit_reason Nullable(String),
    pnl Nullable(Float64),
    pnl_percent Nullable(Float64),
    rejection_reason Nullable(String),
    payload String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(recorded_at)
ORDER BY (trade_id, recorded_at)
"""

CREATE_JOURNAL_ENTRIES = """
CREATE TABLE IF NOT EXISTS journal_entries (
    created_at DateTime64(3, 'UTC'),
    entry_id String,
    trade_id Nullable(String),
    entry_type String,
    market_context String,
    confidence_score Nullable(Float64),
    order_flow_observations String,
    liquidity_observations String,
    amt_observations String,
    lead_lag_observations String,
    mistakes Array(String),
    lessons Array(String),
    improvements Array(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (entry_type, created_at)
"""

ALL_DDL = [CREATE_TRADES, CREATE_JOURNAL_ENTRIES]
EPOCH_TIMESTAMP = datetime(1970, 1, 1, tzinfo=timezone.utc)


class JournalRepository(AITOSModule):
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        username: str = "default",
        password: str = "",
        database: str = "aitos",
    ) -> None:
        self._conn_params = dict(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
        )
        self._client = None
        self._initialized = False
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "journal-repository"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._client = await clickhouse_connect.get_async_client(**self._conn_params)
        for ddl in ALL_DDL:
            await self._client.command(ddl)
        await self._repair_legacy_epoch_trade_timestamps()
        self._initialized = True
        logger.info("JournalRepository initialized (tables ensured)")

    async def _repair_legacy_epoch_trade_timestamps(self) -> None:
        """Repair old snapshots whose recorded_at was persisted as Unix epoch.

        The current writer always supplies an explicit UTC timestamp. Existing
        deployments can nevertheless contain legacy rows written with the
        epoch value. For those rows, exit_time is the best deterministic
        timestamp for a closed snapshot; entry_time is the fallback for open
        snapshots. Rows with neither usable timestamp are intentionally left
        untouched so we never invent historical event times.
        """
        epoch = "toDateTime64(0, 3, 'UTC')"
        exit_ts = "parseDateTimeBestEffortOrNull(nullIf(exit_time, ''))"
        entry_ts = "parseDateTimeBestEffortOrNull(nullIf(entry_time, ''))"
        repairable = f"({exit_ts} IS NOT NULL OR {entry_ts} IS NOT NULL)"
        replacement = f"toDateTime64(coalesce({exit_ts}, {entry_ts}), 3, 'UTC')"
        count_result = await self._client.query(
            f"SELECT count() AS n FROM trades WHERE recorded_at = {epoch} "
            f"AND {repairable}"
        )
        repairable_count = (
            int(count_result.result_rows[0][0]) if count_result.result_rows else 0
        )
        if repairable_count == 0:
            return
        logger.warning(
            "Repairing %s legacy trade snapshots with epoch recorded_at",
            repairable_count,
        )
        await self._client.command(
            "ALTER TABLE trades "
            f"UPDATE recorded_at = {replacement} "
            f"WHERE recorded_at = {epoch} AND {repairable}"
        )
        logger.info("Legacy trade timestamp repair mutation submitted")

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            await self._client.command("SELECT 1")
            latency_ms = (time.monotonic() - start) * 1000
            status = ModuleStatus.HEALTHY
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.monotonic() - start) * 1000
            status = ModuleStatus.UNHEALTHY
            logger.error("journal repository health check failed: %s", exc)
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=latency_ms,
            last_event_time=self._last_event_time,
            details={},
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._client is not None:
            await self._client.close()
        logger.info("JournalRepository shut down")

    async def emit_events(self):
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    # -- Writes -----------------------------------------------------------------

    async def save_trade_snapshot(self, trade_dict: dict[str, Any]) -> None:
        self._require_initialized()
        recorded_at = datetime.now(timezone.utc)
        persisted_payload = dict(trade_dict)
        persisted_payload["recorded_at"] = recorded_at.isoformat()
        await self._client.insert(
            "trades",
            [
                [
                    recorded_at,
                    trade_dict.get("trade_id", ""),
                    trade_dict.get("symbol", ""),
                    trade_dict.get("side", ""),
                    trade_dict.get("entry_price", 0.0),
                    trade_dict.get("quantity", 0.0),
                    trade_dict.get("leverage", 0.0),
                    trade_dict.get("position_size_usd", 0.0),
                    trade_dict.get("risk_amount_usd", 0.0),
                    trade_dict.get("strategy_id", ""),
                    trade_dict.get("sl_price", 0.0),
                    trade_dict.get("tp_price", 0.0),
                    trade_dict.get("state", ""),
                    trade_dict.get("entry_time", ""),
                    trade_dict.get("exit_price"),
                    trade_dict.get("exit_time"),
                    trade_dict.get("exit_reason"),
                    trade_dict.get("pnl"),
                    trade_dict.get("pnl_percent"),
                    trade_dict.get("rejection_reason"),
                    json.dumps(persisted_payload, default=str),
                ]
            ],
            column_names=[
                "recorded_at",
                "trade_id",
                "symbol",
                "side",
                "entry_price",
                "quantity",
                "leverage",
                "position_size_usd",
                "risk_amount_usd",
                "strategy_id",
                "sl_price",
                "tp_price",
                "state",
                "entry_time",
                "exit_price",
                "exit_time",
                "exit_reason",
                "pnl",
                "pnl_percent",
                "rejection_reason",
                "payload",
            ],
        )

    async def save_journal_entry(self, entry: JournalEntry) -> None:
        self._require_initialized()
        await self._client.insert(
            "journal_entries",
            [
                [
                    entry.entry_id,
                    entry.trade_id,
                    entry.entry_type.value,
                    json.dumps(entry.market_context, default=str),
                    entry.confidence_score,
                    json.dumps(entry.order_flow_observations, default=str),
                    json.dumps(entry.liquidity_observations, default=str),
                    json.dumps(entry.amt_observations, default=str),
                    json.dumps(entry.lead_lag_observations, default=str),
                    entry.mistakes,
                    entry.lessons,
                    entry.improvements,
                ]
            ],
            column_names=[
                "entry_id",
                "trade_id",
                "entry_type",
                "market_context",
                "confidence_score",
                "order_flow_observations",
                "liquidity_observations",
                "amt_observations",
                "lead_lag_observations",
                "mistakes",
                "lessons",
                "improvements",
            ],
        )

    # -- Reads --------------------------------------------------------------------

    async def get_journal_entries_for_trade(
        self, trade_id: str
    ) -> list[dict[str, Any]]:
        self._require_initialized()
        result = await self._client.query(
            "SELECT * FROM journal_entries WHERE trade_id = {trade_id:String} ORDER BY created_at",
            parameters={"trade_id": trade_id},
        )
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "JournalRepository.initialize() must be called first"
            )
