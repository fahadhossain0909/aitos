from .ingestion import DataIngestionService, kline_topic, orderbook_topic, trade_topic
from .repository import MarketDataRepository
from .transport_telemetry import install_transport_telemetry

install_transport_telemetry(DataIngestionService)

__all__ = [
    "DataIngestionService",
    "MarketDataRepository",
    "kline_topic",
    "orderbook_topic",
    "trade_topic",
]
