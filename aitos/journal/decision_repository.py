"""Persistent decision journal and outcome attribution store."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import clickhouse_connect

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.logging_setup import get_logger

logger = get_logger("aitos.journal.decision_repository")
CREATE_DECISION_JOURNAL = """
CREATE TABLE IF NOT EXISTS decision_journal (
 recorded_at DateTime64(3, 'UTC'), decision_id String, record_type String,
 trade_id Nullable(String), symbol String, side String, strategy_id String, regime String,
 confidence Nullable(Float64), payload String, pnl Nullable(Float64), pnl_percent Nullable(Float64),
 risk_amount_usd Nullable(Float64), r_multiple Nullable(Float64), holding_seconds Nullable(Float64),
 exit_reason Nullable(String),
evidence_contributions String DEFAULT '[]'
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at) ORDER BY (decision_id, recorded_at)
"""


class DecisionJournalRepository(AITOSModule):
    def __init__(
        self, host=None, port=None, username=None, password=None, database=None
    ):
        host = host or os.getenv("CLICKHOUSE_HOST", "localhost")
        port = int(port or os.getenv("CLICKHOUSE_PORT", "8123"))
        username = username or os.getenv("CLICKHOUSE_USER", "default")
        password = (
            password if password is not None else os.getenv("CLICKHOUSE_PASSWORD", "")
        )
        database = database or os.getenv("CLICKHOUSE_DATABASE", "aitos")
        self._conn_params = dict(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
        )
        self._client = None
        self._initialized = False
        self._last_event_time = None

    @property
    def module_id(self):
        return "decision-journal-repository"

    @property
    def version(self):
        return "1.1.1"

    async def initialize(self, config):
        if self._initialized:
            return
        self._client = await clickhouse_connect.get_async_client(**self._conn_params)
        await self._client.command(CREATE_DECISION_JOURNAL)
        await self._client.command(
            "ALTER TABLE decision_journal ADD COLUMN IF NOT EXISTS evidence_contributions String DEFAULT '[]'"
        )
        self._initialized = True
        logger.info("DecisionJournalRepository initialized")

    async def health_check(self):
        start = time.monotonic()
        try:
            await self._client.command("SELECT 1")
            status = ModuleStatus.HEALTHY
        except Exception as exc:
            status = ModuleStatus.UNHEALTHY
            logger.error("decision journal health check failed: %s", exc)
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=(time.monotonic() - start) * 1000,
            last_event_time=self._last_event_time,
            details={},
        )

    async def shutdown(self, grace_period_seconds=30.0):
        if self._client is not None:
            await self._client.close()

    async def emit_events(self):
        return
        yield

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    async def save_decision(self, decision_id, snapshot):
        self._require_initialized()
        await self._insert(
            "DECISION",
            decision_id,
            None,
            snapshot.get("symbol", ""),
            snapshot.get("side", ""),
            snapshot.get("strategy_id", ""),
            snapshot.get("regime", "unknown"),
            snapshot.get("confidence"),
            snapshot,
            evidence=snapshot.get("evidence_contributions")
            or snapshot.get("contributions")
            or [],
        )

    async def link_trade(self, decision_id, trade):
        self._require_initialized()
        await self._insert(
            "TRADE_LINK",
            decision_id,
            trade.get("trade_id"),
            trade.get("symbol", ""),
            trade.get("side", ""),
            trade.get("strategy_id", ""),
            trade.get("regime", "unknown"),
            None,
            trade,
        )

    async def attribute_outcome(self, decision_id, trade):
        self._require_initialized()
        pnl = trade.get("pnl")
        risk = trade.get("risk_amount_usd")
        r = (
            (float(pnl) / float(risk))
            if pnl is not None and risk not in (None, 0, 0.0)
            else None
        )
        await self._insert(
            "OUTCOME",
            decision_id,
            trade.get("trade_id"),
            trade.get("symbol", ""),
            trade.get("side", ""),
            trade.get("strategy_id", ""),
            trade.get("regime", "unknown"),
            None,
            trade,
            pnl=pnl,
            pnl_percent=trade.get("pnl_percent"),
            risk=risk,
            r_multiple=r,
            holding_seconds=self._holding_seconds(
                trade.get("entry_time"), trade.get("exit_time")
            ),
            exit_reason=trade.get("exit_reason"),
        )

    async def get_records(self, decision_id):
        self._require_initialized()
        result = await self._client.query(
            "SELECT * FROM decision_journal WHERE decision_id = {decision_id:String} ORDER BY recorded_at",
            parameters={"decision_id": decision_id},
        )
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    async def _insert(
        self,
        record_type,
        decision_id,
        trade_id,
        symbol,
        side,
        strategy_id,
        regime,
        confidence,
        payload,
        pnl=None,
        pnl_percent=None,
        risk=None,
        r_multiple=None,
        holding_seconds=None,
        exit_reason=None,
        evidence=None,
    ):
        recorded_at = datetime.now(timezone.utc).replace(tzinfo=None)
        evidence = evidence or []
        await self._client.insert(
            "decision_journal",
            [
                [
                    recorded_at,
                    decision_id,
                    record_type,
                    trade_id,
                    str(symbol),
                    str(side),
                    str(strategy_id),
                    str(regime),
                    confidence,
                    json.dumps(payload, default=str),
                    pnl,
                    pnl_percent,
                    risk,
                    r_multiple,
                    holding_seconds,
                    exit_reason,
                    json.dumps(evidence, default=str),
                ]
            ],
            column_names=[
                "recorded_at",
                "decision_id",
                "record_type",
                "trade_id",
                "symbol",
                "side",
                "strategy_id",
                "regime",
                "confidence",
                "payload",
                "pnl",
                "pnl_percent",
                "risk_amount_usd",
                "r_multiple",
                "holding_seconds",
                "exit_reason",
                "evidence_contributions",
            ],
        )
        self._last_event_time = recorded_at.isoformat()

    @staticmethod
    def _holding_seconds(entry_time, exit_time):
        if not entry_time or not exit_time:
            return None
        try:
            return max(
                0.0,
                (
                    datetime.fromisoformat(exit_time)
                    - datetime.fromisoformat(entry_time)
                ).total_seconds(),
            )
        except (TypeError, ValueError):
            return None

    def _require_initialized(self):
        if not self._initialized:
            raise ModuleNotInitializedError(
                "DecisionJournalRepository.initialize() must be called first"
            )
