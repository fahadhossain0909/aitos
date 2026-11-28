"""Canonical market-data gateway boundary."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aitos.logging_setup import get_logger

from .backpressure import BoundedMarketQueue
from .contracts import MarketEvent, MarketEventType, MarketSource
from .gateway_health import GatewayHealth

logger = get_logger("aitos.market_data.gateway")


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
    max_queue_age_seconds: float = 10.0
    publish_timeout_seconds: float = 5.0
    backpressure_poll_seconds: float = 0.005
    publish_batch_size: int = 64


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
        publisher_owner = getattr(publisher, "__self__", None)
        self._batch_publisher = getattr(publisher_owner, "publish_many", None)
        self._drain_lock = asyncio.Lock()
        self._transport_snapshot_provider = transport_snapshot_provider

    def begin_connect(self) -> None:
        self.state = GatewayState.CONNECTING
        self.health.connected = False
        self.health.degraded = False

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
        ingest_delay_ms = max(
            0.0, (event.ingest_time - event.event_time).total_seconds() * 1000.0
        )
        post_ingest_age_ms = max(
            0.0,
            (datetime.now(timezone.utc) - event.ingest_time).total_seconds() * 1000.0,
        )
        stage_timestamps = dict(event.stage_timestamps or {})
        frame_received_monotonic = stage_timestamps.get("ws_frame_received_monotonic")
        decode_started_monotonic = stage_timestamps.get("ws_decode_started_monotonic")
        decode_completed_monotonic = stage_timestamps.get(
            "ws_decode_completed_monotonic"
        )
        normalized_monotonic = stage_timestamps.get("event_normalized_monotonic")
        frame_received_epoch_ms = stage_timestamps.get("ws_frame_received_epoch_ms")
        now_monotonic = time.monotonic()
        source_to_receive_ms = (
            frame_received_epoch_ms - event.event_time.timestamp() * 1000.0
            if isinstance(frame_received_epoch_ms, (int, float))
            else None
        )
        receive_to_gateway_ms = (
            max(0.0, now_monotonic - frame_received_monotonic) * 1000.0
            if isinstance(frame_received_monotonic, (int, float))
            else None
        )
        decode_duration_ms = (
            max(0.0, decode_completed_monotonic - decode_started_monotonic) * 1000.0
            if isinstance(decode_started_monotonic, (int, float))
            and isinstance(decode_completed_monotonic, (int, float))
            else None
        )
        normalize_duration_ms = (
            max(0.0, normalized_monotonic - decode_completed_monotonic) * 1000.0
            if isinstance(normalized_monotonic, (int, float))
            and isinstance(decode_completed_monotonic, (int, float))
            else None
        )
        try:
            from aitos.forensics.data_flow_hooks import note_trace, record_input
            from aitos.forensics.data_flow_schema import PipelineStage

            record_input(
                PipelineStage.GATEWAY_QUEUE.value,
                symbol=symbol,
                source_age_ms=age * 1000.0,
            )
            note_trace(
                trace_id=event.trace_id,
                stage=PipelineStage.GATEWAY_QUEUE.value,
                symbol=symbol,
                event_id=event.event_id,
                extra={
                    "phase": "ingress",
                    "event_age_ms": age * 1000.0,
                    "source_to_receive_ms": source_to_receive_ms,
                    "receive_to_gateway_ms": receive_to_gateway_ms,
                    "websocket_decode_ms": decode_duration_ms,
                    "decode_to_normalize_ms": normalize_duration_ms,
                    "source_to_ingest_ms": ingest_delay_ms,
                    "ingest_to_gateway_ms": post_ingest_age_ms,
                },
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
                from aitos.forensics.data_flow_schema import (
                    DataIssueClass,
                    PipelineStage,
                )

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
        sequence_critical = event.event_type in {
            MarketEventType.BOOK_DELTA,
            MarketEventType.BOOK_SNAPSHOT,
        }
        can_replace = lambda queued: queued.event_type not in {
            MarketEventType.BOOK_DELTA,
            MarketEventType.BOOK_SNAPSHOT,
        }
        if event.event_type == MarketEventType.TICKER:
            accepted = self.queue.put_coalesced_nowait(
                event,
                key=lambda queued: str(queued.symbol or "").upper(),
                can_replace=lambda queued: queued.event_type
                not in {
                    MarketEventType.BOOK_DELTA,
                    MarketEventType.BOOK_SNAPSHOT,
                    MarketEventType.TRADE,
                },
            )
        else:
            accepted = self.queue.put_latest_nowait(event, can_replace=can_replace)
        if accepted:
            self.health.record_accept()
            symbol = self._data_flow_symbol(event)
            try:
                from aitos.forensics.data_flow_hooks import (
                    note_trace,
                    record_output,
                    set_queue,
                )
                from aitos.forensics.data_flow_schema import PipelineStage

                record_output(PipelineStage.GATEWAY_QUEUE.value, symbol=symbol)
                note_trace(
                    trace_id=event.trace_id,
                    stage=PipelineStage.GATEWAY_QUEUE.value,
                    symbol=symbol,
                    event_id=event.event_id,
                    extra={"phase": "enqueued"},
                )
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
        if sequence_critical:
            # A dropped sequence-critical event invalidates incremental book
            # continuity. Mark the gateway degraded so downstream recovery can
            # request a fresh snapshot; never evict an older book delta to hide it.
            self.state = GatewayState.DEGRADED
            self.health.degraded = True
            self.health.record_reject(
                "orderbook_queue_saturated",
                "sequence-critical event rejected; order-book resynchronization required",
            )
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
        """Serialize bounded queue drains to preserve FIFO publication order."""
        async with self._drain_lock:
            return await self._drain_once_locked()

    async def _drain_once_locked(self) -> bool:
        """Publish a bounded event batch, dropping only events that are stale."""
        queue_started = time.perf_counter()
        events: list[MarketEvent] = []
        try:
            first = await asyncio.wait_for(self.queue.get(), timeout=0.01)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        events.append(first)
        for _ in range(max(0, self.config.publish_batch_size - 1)):
            try:
                events.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        self._record_drain_stage("queue_get", queue_started)
        publishable: list[MarketEvent] = []
        for event in events:
            symbol = self._data_flow_symbol(event)
            try:
                from aitos.forensics.data_flow_hooks import record_input
                from aitos.forensics.data_flow_schema import PipelineStage

                record_input(PipelineStage.REDIS_PUBLISH.value, symbol=symbol)
            except Exception:
                pass
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
                continue
            publishable.append(event)
        try:
            if not publishable:
                return False
            publish_started = time.perf_counter()
            try:
                if self._batch_publisher is not None and len(publishable) > 1:
                    await asyncio.wait_for(
                        self._batch_publisher(publishable),
                        timeout=self.config.publish_timeout_seconds,
                    )
                else:
                    for event in publishable:
                        await asyncio.wait_for(
                            self._publisher(event),
                            timeout=self.config.publish_timeout_seconds,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                for event in publishable:
                    symbol = self._data_flow_symbol(event)
                    self.health.record_publish_error(str(exc))
                    self.health.dropped_events += 1
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
                self.state = GatewayState.DEGRADED
                logger.warning(
                    "gateway batch publish failed",
                    extra={
                        "aitos_extra": {
                            "stage": "gateway_publish_error",
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "batch_size": len(publishable),
                        }
                    },
                )
                raise
            finally:
                self._record_drain_stage("publisher", publish_started)
            latency_ms = (time.perf_counter() - queue_started) * 1000.0
            for event in publishable:
                self.health.record_publish()
                try:
                    from aitos.forensics.data_flow_hooks import (
                        note_trace,
                        record_output,
                    )
                    from aitos.forensics.data_flow_schema import PipelineStage

                    symbol = self._data_flow_symbol(event)
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
                        event_id=event.event_id,
                        extra={"phase": "gateway_drain_to_publish"},
                    )
                except Exception:
                    pass
            return True
        finally:
            task_started = time.perf_counter()
            for _event in events:
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
                "publish_batch_size": self.config.publish_batch_size,
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
