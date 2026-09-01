from .ingestion import DataIngestionService, kline_topic, orderbook_topic, trade_topic
from .repository import MarketDataRepository
from .trade_recovery_guard import install_trade_recovery_guard
from .transport_telemetry import install_transport_telemetry

install_transport_telemetry(DataIngestionService)
install_trade_recovery_guard(DataIngestionService)

__all__ = [
    "DataIngestionService",
    "MarketDataRepository",
    "kline_topic",
    "orderbook_topic",
    "trade_topic",
]
