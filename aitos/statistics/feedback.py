"""Durable outcome feedback for A-Stat probability calibration."""

from __future__ import annotations

import asyncio
import math
import sqlite3
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.eventbus.contracts import EventBusProtocol, SubscriptionProtocol
from aitos.statistics.engine import AStatEngine


class AStatFeedbackStore:
    """Idempotent SQLite journal of calibrated forecast/outcome pairs."""

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = 10000):
        self.database_path = str(database_path)
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))

    def _connect(self):
        connection = sqlite3.connect(
            self.database_path, timeout=self.busy_timeout_ms / 1000
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS a_stat_feedback (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    predicted_up REAL NOT NULL,
                    realised_return REAL NOT NULL,
                    resolved_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_a_stat_feedback_resolved "
                "ON a_stat_feedback(resolved_at, trade_id)"
            )

    def record(
        self,
        trade_id: str,
        *,
        symbol: str,
        predicted_up: float,
        realised_return: float,
        resolved_at: str,
    ) -> bool:
        probability = float(predicted_up)
        result = float(realised_return)
        if not trade_id.strip() or not symbol.strip():
            raise ValueError("trade_id and symbol are required")
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("predicted_up must be finite and within [0, 1]")
        if not math.isfinite(result):
            raise ValueError("realised_return must be finite")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO a_stat_feedback
                (trade_id, symbol, predicted_up, realised_return, resolved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (trade_id, symbol.upper(), probability, result, resolved_at),
            )
            return cursor.rowcount == 1

    def load_recent(self, limit: int) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_id, symbol, predicted_up, realised_return, resolved_at
                FROM a_stat_feedback
                ORDER BY resolved_at DESC, trade_id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return tuple(dict(row) for row in reversed(rows))

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM a_stat_feedback"
            ).fetchone()
        return int(row["total"] or 0)


class AStatFeedbackLoop(AITOSModule):
    """Restore and update A-Stat calibration from net closed-trade outcomes.

    The forecast probability is directional relative to the proposed trade
    side. Trade PnL is also side-normalized by the lifecycle, so positive net
    return is the realized success label for either LONG or SHORT.
    """

    def __init__(
        self,
        event_bus: EventBusProtocol,
        engine: AStatEngine,
        database_path: str | Path,
        *,
        calibration_window: int = 256,
    ) -> None:
        if calibration_window <= 0:
            raise ValueError("calibration_window must be positive")
        self._event_bus = event_bus
        self._engine = engine
        self._store = AStatFeedbackStore(database_path)
        self._window = int(calibration_window)
        self._lock = asyncio.Lock()
        self._subscriptions: list[SubscriptionProtocol] = []
        self._initialized = False
        self._updates_applied = 0
        self._updates_restored = 0
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "a-stat-feedback-loop"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        await asyncio.to_thread(self._store.initialize)
        # Subscribe while holding the same lock used by the handler. Events
        # arriving during restoration wait here and cannot be lost or double
        # applied between the durable replay and subscription activation.
        async with self._lock:
            rows = await asyncio.to_thread(self._store.load_recent, self._window)
            for row in rows:
                self._engine.update(
                    float(row["realised_return"]), float(row["predicted_up"])
                )
            self._updates_restored = len(rows)
            self._subscriptions = [
                await self._event_bus.subscribe(
                    "trade.position_closed",
                    self._on_position_closed,
                    group="a-stat-feedback",
                )
            ]
            self._initialized = True

    async def _on_position_closed(
        self, event: Event
    ) -> EventResponse | None:
        payload = event.payload
        if not isinstance(payload, Mapping):
            return None
        consensus = payload.get("agent_consensus")
        forecast = (
            consensus.get("statistical_forecast")
            if isinstance(consensus, Mapping)
            else None
        )
        if not isinstance(forecast, Mapping):
            return None
        trade_id = str(payload.get("trade_id") or "").strip()
        symbol = str(payload.get("symbol") or "").strip()
        if not trade_id or not symbol:
            return None
        try:
            predicted_up = float(forecast["predicted_up"])
            pnl_percent = float(payload["pnl_percent"])
            realised_return = pnl_percent / 100.0
            resolved_at = str(
                payload.get("exit_time")
                or event.created_at
                or datetime.now(timezone.utc).isoformat()
            )
            if (
                not math.isfinite(predicted_up)
                or not 0.0 <= predicted_up <= 1.0
                or not math.isfinite(realised_return)
            ):
                return None
        except (KeyError, TypeError, ValueError):
            return None

        async with self._lock:
            inserted = await asyncio.to_thread(
                self._store.record,
                trade_id,
                symbol=symbol,
                predicted_up=predicted_up,
                realised_return=realised_return,
                resolved_at=resolved_at,
            )
            if not inserted:
                return None
            self._engine.update(realised_return, predicted_up)
            self._updates_applied += 1
            self._last_event_time = resolved_at
        return None

    async def health_check(self) -> HealthStatus:
        total = await asyncio.to_thread(self._store.count) if self._initialized else 0
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "updates_applied": self._updates_applied,
                "updates_restored": self._updates_restored,
                "durable_outcomes": total,
                "calibration_window": self._window,
                "source": "closed_trade_statistical_forecast",
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()
        self._initialized = False

    async def emit_events(self) -> AsyncIterator[Event]:
        if False:
            yield Event(topic="noop", payload={}, source_module=self.module_id)

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None
