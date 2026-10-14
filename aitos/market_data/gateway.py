"""Canonical market-data gateway boundary."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .backpressure import BoundedMarketQueue
from .contracts import MarketEvent, MarketSource
from .gateway_health import GatewayHealth


class GatewayState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    queue_capacity: int = 8192
    max_source_age_seconds: float = 15.0
    max_queue_age_seconds: float = 1.0
    publish_timeout_seconds: float = 1.0
    backpressure_poll_seconds: float = 0.005


class MarketDataGateway:
    """Transport-independent gateway with freshness-first backpressure."""

    def __init__(
        self,
        venue: str,
        market_type: str,
        publisher: Any,
        config: GatewayConfig | None = None,
        transport_snapshot_provider: Any | None = None,
    ) -> None:
        self.config = config or GatewayConfig()
        self.queue = BoundedMarketQueue[MarketEvent](self.config.queue_capacity)
        self.health = GatewayHealth(venue, market_type)
        self.state = GatewayState.STOPPED
        self._publisher = publisher
        self._transport_snapshot_provider = transport_snapshot_provider

    def begin_connect(self) -> None:
        self.state = GatewayState.CONNECTING
        self.health.connected = False

    def mark_connected(self) -> None:
        self.state = GatewayState.CONNECTED
        self.health.connected = True
        self.health.degraded = False

    def mark_reconnecting(self) -> None:
        self.state = GatewayState.RECONNECTING
        self.health.reconnect()

    def stop(self) -> None:
        self.state = GatewayState.STOPPED
        self.health.connected = False

    @staticmethod
    def _source_freshness_age_seconds(event: MarketEvent) -> float:
        """Return the wall-clock age of the exchange event itself."""
        return max(0.0, (datetime.now(timezone.utc) - event.event_time).total_seconds())

    def _data_flow_symbol(self, event: MarketEvent) -> str:
        return str(getattr(event, "symbol", "") or "")

    def _validate_event(self, event: MarketEvent) -> bool:
        self.health.record_event()
        age = self._source_freshness_age_seconds(event)
        symbol = self._data_flow_symbol(event)
        try:
            from aitos.forensics.data_flow_hooks import record_input
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(
                PipelineStage.GATEWAY_QUEUE.value,
                symbol=symbol,
                source_age_ms=age * 1000.0,
            )
        except Exception:
            pass
        if (
            event.source == MarketSource.WEBSOCKET
            and age > self.config.max_source_age_seconds
        ):
            self.health.record_reject(
                "stale_websocket", f"source age {age:.3f}s exceeded limit"
            )
            self.state = GatewayState.DEGRADED
            try:
                from aitos.forensics.data_flow_hooks import record_drop
                from aitos.forensics.data_flow_schema import DataIssueClass, PipelineStage

                record_drop(
                    PipelineStage.GATEWAY_QUEUE.value,
                    symbol=symbol,
                    issue=DataIssueClass.STALE.value,
                )
            except Exception:
                pass
            return False
        if event.source == MarketSource.REST:
            self.health.degraded = True
            if self.state == GatewayState.CONNECTED:
                self.state = GatewayState.DEGRADED
        return True

    def _accept_latest(self, event: MarketEvent) -> bool:
        replaced_before = self.queue.stats.replaced_oldest
        if self.queue.put_latest_nowait(event):
            self.health.record_accept()
            symbol = self._data_flow_symbol(event)
            try:
                from aitos.forensics.data_flow_hooks import record_output, set_queue
                from aitos.forensics.data_flow_schema import PipelineStage

                record_output(PipelineStage.GATEWAY_QUEUE.value, symbol=symbol)
                qsnap = self.queue.snapshot()
                depth = int(qsnap.get("depth", qsnap.get("size", 0)) or 0)
                set_queue(PipelineStage.GATEWAY_QUEUE.value, depth=depth, symbol=symbol)
            except Exception:
                pass
            if self.queue.stats.replaced_oldest > replaced_before:
                self.health.backpressure_events += 1
                self.health.dropped_events += 1
                try:
                    from aitos.forensics.data_flow_hooks import record_drop
                    from aitos.forensics.data_flow_schema import (
                        DataIssueClass,
                        PipelineStage,
                    )

                    record_drop(
                        PipelineStage.GATEWAY_QUEUE.value,
                        symbol=self._data_flow_symbol(event),
                        issue=DataIssueClass.DROPPED_BACKPRESSURE.value,
                    )
                except Exception:
                    pass
            return True
        self.health.record_freshness_drop("unable to enqueue newest market event")
        try:
            from aitos.forensics.data_flow_hooks import record_drop
            from aitos.forensics.data_flow_schema import DataIssueClass, PipelineStage

            record_drop(
                PipelineStage.GATEWAY_QUEUE.value,
                symbol=self._data_flow_symbol(event),
                issue=DataIssueClass.DROPPED_BACKPRESSURE.value,
            )
        except Exception:
            pass
        return False

    def accept(self, event: MarketEvent) -> bool:
        """Accept synchronously without allowing an old queue to stall live data."""
        if not self._validate_event(event):
            return False
        return self._accept_latest(event)

    async def accept_async(self, event: MarketEvent) -> bool:
        """Accept immediately; replace old queued data instead of waiting."""
        if not self._validate_event(event):
            return False
        return self._accept_latest(event)

    def _stopped_for_accept(self) -> bool:
        return self.state is GatewayState.STOPPED

    @staticmethod
    def _queue_age_seconds(event: MarketEvent) -> float:
        return max(
            0.0, (datetime.now(timezone.utc) - event.ingest_time).total_seconds()
        )

    def _record_drain_stage(self, stage: str, started: float) -> None:
        recorder = getattr(self, "record_drain_stage", None)
        if recorder is not None:
            try:
                recorder(stage, (time.perf_counter() - started) * 1000)
            except Exception:
                pass

    async def drain_once(self) -> bool:
        """Publish one event, or drop it if doing so would violate freshness."""
        queue_started = time.perf_counter()
        event = await self.queue.get()
        self._record_drain_stage("queue_get", queue_started)
        symbol = self._data_flow_symbol(event)
        try:
            from aitos.forensics.data_flow_hooks import record_input
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(PipelineStage.REDIS_PUBLISH.value, symbol=symbol)
        except Exception:
            pass
        try:
            age_started = time.perf_counter()
            queue_age = self._queue_age_seconds(event)
            self._record_drain_stage("queue_age_check", age_started)
            if queue_age > self.config.max_queue_age_seconds:
                self.health.record_freshness_drop(
                    f"queued market event age {queue_age:.3f}s exceeded "
                    f"{self.config.max_queue_age_seconds:.3f}s"
                )
                try:
                    from aitos.forensics.data_flow_hooks import record_drop
                    from aitos.forensics.data_flow_schema import (
                        DataIssueClass,
                        PipelineStage,
                    )

                    record_drop(
                        PipelineStage.REDIS_PUBLISH.value,
                        symbol=symbol,
                        issue=DataIssueClass.STALE.value,
                    )
                except Exception:
                    pass
                return False
            try:
                publish_started = time.perf_counter()
                try:
                    await asyncio.wait_for(
                        self._publisher(event),
                        timeout=self.config.publish_timeout_seconds,
                    )
                finally:
                    self._record_drain_stage("publisher", publish_started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.record_publish_error(str(exc))
                self.health.dropped_events += 1
                self.state = GatewayState.DEGRADED
                try:
                    from aitos.forensics.data_flow_hooks import record_error
                    from aitos.forensics.data_flow_schema import PipelineStage

                    record_error(
                        PipelineStage.REDIS_PUBLISH.value,
                        symbol=symbol,
                        issue="publish_error",
                    )
                except Exception:
                    pass
                raise
            else:
                self.health.record_publish()
                latency_ms = (time.perf_counter() - queue_started) * 1000.0
                try:
                    from aitos.forensics.data_flow_hooks import note_trace, record_output
                    from aitos.forensics.data_flow_schema import PipelineStage

                    record_output(
                        PipelineStage.REDIS_PUBLISH.value,
                        symbol=symbol,
                        latency_ms=latency_ms,
                    )
                    note_trace(
                        trace_id=getattr(event, "trace_id", None),
                        stage=PipelineStage.REDIS_PUBLISH.value,
                        symbol=symbol,
                        latency_ms=latency_ms,
                    )
                except Exception:
                    pass
                return True
        finally:
            task_started = time.perf_counter()
            self.queue.task_done()
            self._record_drain_stage("queue_task_done", task_started)

    def snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "state": self.state.value,
            "queue": self.queue.snapshot(),
            "health": self.health.snapshot(),
            "freshness_policy": {
                "max_queue_age_seconds": self.config.max_queue_age_seconds,
                "publish_timeout_seconds": self.config.publish_timeout_seconds,
                "overflow": "replace_oldest_with_newest",
                "failed_publish": "drop_failed_event",
            },
        }
        if self._transport_snapshot_provider is not None:
            try:
                snapshot["transport"] = self._transport_snapshot_provider()
            except Exception as exc:
                snapshot["transport"] = {
                    "state": "telemetry_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
        return snapshot
