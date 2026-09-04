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

__all__ = ["DLQ_STREAM", "EventBus", "Subscription", "validate_event_schema"]
