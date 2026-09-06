"""Data package exports with lazy ingestion compatibility imports."""

from __future__ import annotations

from .repository import MarketDataRepository
from .trade_recovery_guard import install_trade_recovery_guard
from .transport_telemetry import install_transport_telemetry

__all__ = [
    "DataIngestionService",
    "MarketDataRepository",
    "kline_topic",
    "liquidity_topic",
    "live_state_topic",
    "orderbook_topic",
    "orderflow_topic",
    "trade_topic",
]


def __getattr__(name: str):
    """Lazily expose ingestion compatibility symbols.

    ``persistence_sink`` imports ``aitos.data.repository``. Importing the
    ingestion facade eagerly from this package initializer would then load
    ``ingestion.py``, which imports ``persistence_sink`` again and creates a
    circular import during module initialization.
    """
    if name in __all__ and name != "MarketDataRepository":
        from .ingestion import (
            DataIngestionService,
            kline_topic,
            liquidity_topic,
            live_state_topic,
            orderbook_topic,
            orderflow_topic,
            trade_topic,
        )

        exports = {
            "DataIngestionService": DataIngestionService,
            "kline_topic": kline_topic,
            "liquidity_topic": liquidity_topic,
            "live_state_topic": live_state_topic,
            "orderbook_topic": orderbook_topic,
            "orderflow_topic": orderflow_topic,
            "trade_topic": trade_topic,
        }
        value = exports[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
