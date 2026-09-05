import asyncio

from aitos.forensics.market_data_attribution import install_eventbus_attribution
from aitos.forensics.pipeline_stage_telemetry import (
    install as install_pipeline_stage_telemetry,
)
from aitos.forensics.safe_market_data_telemetry import (
    install as install_safe_market_data_telemetry,
)
from aitos.forensics.scanner_performance_telemetry import (
    install as install_scanner_performance_telemetry,
)

from .consumer_concurrency import install_eventbus_consumer_concurrency
from .redis_bus import DLQ_STREAM, EventBus, Subscription, validate_event_schema

install_eventbus_consumer_concurrency(EventBus)
install_eventbus_attribution(EventBus)
install_safe_market_data_telemetry(EventBus)
install_pipeline_stage_telemetry()
install_scanner_performance_telemetry()


_original_subscribe = EventBus.subscribe


async def _subscribe_with_live_only(self, *args, **kwargs):
    """Accept the explicit live-only contract while preserving start_id API."""
    live_only = kwargs.pop("live_only", None)
    if live_only is True and "start_id" not in kwargs:
        kwargs["start_id"] = "$"
    subscription = await _original_subscribe(self, *args, **kwargs)
    if live_only is True:
        # Let the consumer task establish/reset its live cursor before the
        # caller can publish the first event. Without this scheduling point,
        # a just-created wildcard subscription could move its group to '$'
        # after that first event was appended, losing it from the live stream.
        await asyncio.sleep(0)
    return subscription


EventBus.subscribe = _subscribe_with_live_only

__all__ = ["DLQ_STREAM", "EventBus", "Subscription", "validate_event_schema"]
