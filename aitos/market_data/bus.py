"""Canonical market-data bus over Redis Streams."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aitos.core.contracts import Event
from aitos.eventbus import redis_bus as redis_bus_module
from aitos.eventbus.redis_bus import EventBus, Subscription

from .channels import (
    CHANNEL_BOOK_DELTA,
    CHANNEL_BOOK_SNAPSHOT,
    CHANNEL_FUNDING,
    CHANNEL_INSTRUMENT,
    CHANNEL_LIQUIDATION,
    CHANNEL_OPEN_INTEREST,
    CHANNEL_OPTIONS,
    CHANNEL_TICKER,
    CHANNEL_TRADE,
)
from .contracts import MarketEvent, MarketEventType, MarketSource
from .retention import maxlen_for

_CHANNEL_BY_TYPE = {
    MarketEventType.TRADE: CHANNEL_TRADE,
    MarketEventType.BOOK_DELTA: CHANNEL_BOOK_DELTA,
    MarketEventType.BOOK_SNAPSHOT: CHANNEL_BOOK_SNAPSHOT,
    MarketEventType.TICKER: CHANNEL_TICKER,
    MarketEventType.KLINE: "market.kline",
    MarketEventType.FUNDING: CHANNEL_FUNDING,
    MarketEventType.OPEN_INTEREST: CHANNEL_OPEN_INTEREST,
    MarketEventType.LIQUIDATION: CHANNEL_LIQUIDATION,
    MarketEventType.OPTIONS: CHANNEL_OPTIONS,
    MarketEventType.INSTRUMENT: CHANNEL_INSTRUMENT,
}

# The legacy EventBus is intentionally retained as the transport primitive during
# migration. Register v1 semantic channels with its bounded-retention policy so
# a canonical stream can never grow without limit.
for _channel in set(_CHANNEL_BY_TYPE.values()):
    redis_bus_module.STREAM_MAXLEN_DEFAULTS.setdefault(_channel, maxlen_for(_channel))


def channel_for(event_type: MarketEventType) -> str:
    try:
        return _CHANNEL_BY_TYPE[event_type]
    except KeyError as exc:
        raise ValueError(f"unsupported market event type: {event_type}") from exc


def _datetime_from_wire(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def market_event_to_wire(event: MarketEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type.value,
        "exchange": event.exchange,
        "venue": event.venue,
        "market": event.market,
        "market_type": event.market_type,
        "symbol": event.symbol,
        "instrument_id": event.instrument_id,
        "event_time": event.event_time.isoformat(),
        "source_ts": event.source_ts.isoformat(),
        "ingest_time": event.ingest_time.isoformat(),
        "received_ts": event.received_ts.isoformat(),
        "source": event.source.value,
        "event_id": event.event_id,
        "sequence": event.sequence,
        "correlation_id": event.correlation_id,
        "trace_id": event.trace_id,
        "schema_version": event.schema_version,
        "payload": event.payload,
    }


def market_event_from_wire(payload: dict[str, Any]) -> MarketEvent:
    event_time_raw = payload.get("event_time") or payload.get("source_ts")
    ingest_time_raw = payload.get("ingest_time") or payload.get("received_ts")
    if event_time_raw is None or ingest_time_raw is None:
        raise ValueError(
            "market event wire payload requires event/source and ingest/received timestamps"
        )
    event_time = _datetime_from_wire(event_time_raw)
    ingest_time = _datetime_from_wire(ingest_time_raw)
    return MarketEvent(
        event_type=MarketEventType(payload["event_type"]),
        exchange=str(payload["exchange"]),
        venue=str(payload.get("venue") or payload["exchange"]),
        market=str(payload["market"]),
        market_type=str(payload.get("market_type") or payload["market"]),
        symbol=str(payload["symbol"]),
        instrument_id=str(payload.get("instrument_id") or payload["symbol"]),
        event_time=event_time,
        payload=dict(payload.get("payload") or {}),
        source=MarketSource(payload["source"]),
        ingest_time=ingest_time,
        event_id=str(payload["event_id"]),
        sequence=(
            int(payload["sequence"]) if payload.get("sequence") is not None else None
        ),
        correlation_id=payload.get("correlation_id"),
        trace_id=payload.get("trace_id"),
        schema_version=int(payload.get("schema_version", 1)),
    )


MarketEventHandler = Callable[[MarketEvent], Awaitable[Any]]


class MarketDataBus:
    """Semantic market-data API backed by Redis Streams."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    async def publish(self, event: MarketEvent) -> None:
        channel = channel_for(event.event_type)
        payload = market_event_to_wire(event)
        # Production contract checkpoint. Findings are observed and classified;
        # the checkpoint does not silently discard traffic during rollout.
        try:
            from aitos.forensics.data_contract import (
                check_orderbook_payload,
                check_trade_payload,
            )
            from aitos.forensics.data_flow_hooks import (
                note_trace,
                record_error,
                record_input,
                record_output,
            )
            from aitos.forensics.data_flow_schema import DataIssueClass, PipelineStage

            symbol = str(event.symbol or "")
            record_input(
                PipelineStage.NORMALIZE.value,
                symbol=symbol,
                source_age_ms=event.source_age_seconds * 1000.0,
            )
            record_output(PipelineStage.NORMALIZE.value, symbol=symbol)
            note_trace(
                trace_id=event.trace_id,
                stage=PipelineStage.NORMALIZE.value,
                symbol=symbol,
                event_id=event.event_id,
                extra={"event_type": event.event_type.value},
            )
            result = None
            checked_payload = dict(event.payload)
            checked_payload.setdefault("symbol", event.symbol)
            if event.event_type == MarketEventType.TRADE:
                checked_payload.setdefault("T", event.event_time.timestamp() * 1000.0)
                result = check_trade_payload(
                    checked_payload, now_ms=time.time() * 1000.0
                )
            elif event.event_type in (
                MarketEventType.BOOK_SNAPSHOT,
                MarketEventType.BOOK_DELTA,
            ):
                result = check_orderbook_payload(checked_payload)
            if result is not None and not result.ok:
                record_error(
                    PipelineStage.NORMALIZE.value,
                    symbol=symbol,
                    issue=result.issue.value,
                )
                note_trace(
                    trace_id=event.trace_id,
                    stage=PipelineStage.NORMALIZE.value,
                    symbol=symbol,
                    event_id=event.event_id,
                    extra={
                        "contract_ok": False,
                        "issue": result.issue.value,
                        "detail": result.detail,
                    },
                )
            elif result is not None and result.issue != DataIssueClass.NONE:
                record_error(
                    PipelineStage.NORMALIZE.value,
                    symbol=symbol,
                    issue=result.issue.value,
                )
        except Exception:
            # Telemetry and validation must never interrupt the trading path.
            pass
        payload["retention_maxlen"] = maxlen_for(channel)
        symbol = str(event.symbol or "")
        started = time.perf_counter()
        try:
            from aitos.forensics.data_flow_hooks import record_input
            from aitos.forensics.data_flow_schema import PipelineStage

            # Downstream of gateway drain: consumer-side ingress onto Redis stream.
            record_input(PipelineStage.REDIS_PUBLISH.value, symbol=symbol)
        except Exception:
            pass
        await self._event_bus.publish(
            Event(
                topic=channel,
                payload=payload,
                event_id=event.event_id,
                source_module="market-data-gateway",
                correlation_id=event.correlation_id,
                schema_version=event.schema_version,
            )
        )
        try:
            from aitos.forensics.data_flow_hooks import note_trace, record_output
            from aitos.forensics.data_flow_schema import PipelineStage

            latency_ms = (time.perf_counter() - started) * 1000.0
            record_output(
                PipelineStage.REDIS_PUBLISH.value,
                symbol=symbol,
                latency_ms=latency_ms,
            )
            note_trace(
                trace_id=event.trace_id,
                stage=PipelineStage.REDIS_PUBLISH.value,
                symbol=symbol,
                latency_ms=latency_ms,
                event_id=event.event_id,
                extra={
                    "event_type": event.event_type.value,
                    "source": event.source.value,
                },
            )
        except Exception:
            pass

    async def subscribe(
        self,
        event_type: MarketEventType,
        handler: MarketEventHandler,
        *,
        group: str,
        live_only: bool = True,
    ) -> Subscription:
        channel = channel_for(event_type)

        async def _handle(event: Event) -> Any:
            started = time.perf_counter()
            market_event = market_event_from_wire(event.payload)
            symbol = str(market_event.symbol or "")
            try:
                from aitos.forensics.data_flow_hooks import note_trace, record_input
                from aitos.forensics.data_flow_schema import PipelineStage

                record_input(PipelineStage.REDIS_CONSUMER.value, symbol=symbol)
                note_trace(
                    trace_id=market_event.trace_id,
                    stage=PipelineStage.REDIS_CONSUMER.value,
                    symbol=symbol,
                    event_id=market_event.event_id,
                    extra={
                        "event_type": market_event.event_type.value,
                        "source": market_event.source.value,
                    },
                )
            except Exception:
                pass
            try:
                result = await handler(market_event)
            except Exception:
                try:
                    from aitos.forensics.data_flow_hooks import record_error
                    from aitos.forensics.data_flow_schema import PipelineStage

                    record_error(
                        PipelineStage.REDIS_CONSUMER.value,
                        symbol=symbol,
                        issue="handler_error",
                    )
                except Exception:
                    pass
                raise
            try:
                from aitos.forensics.data_flow_hooks import record_output
                from aitos.forensics.data_flow_schema import PipelineStage

                consumer_latency_ms = (time.perf_counter() - started) * 1000.0
                record_output(
                    PipelineStage.REDIS_CONSUMER.value,
                    symbol=symbol,
                    latency_ms=consumer_latency_ms,
                )
                note_trace(
                    trace_id=market_event.trace_id,
                    stage=PipelineStage.REDIS_CONSUMER.value,
                    symbol=symbol,
                    latency_ms=consumer_latency_ms,
                    event_id=market_event.event_id,
                )
            except Exception:
                pass
            return result

        return await self._event_bus.subscribe(
            channel,
            _handle,
            group=group,
            start_id="$" if live_only else "0",
        )
