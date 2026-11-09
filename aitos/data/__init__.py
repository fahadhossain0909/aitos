"""Data package exports with lazy imports for lightweight adapter processes."""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "MarketDataRepository": (".repository", "MarketDataRepository"),
    "install_trade_recovery_guard": (
        ".trade_recovery_guard",
        "install_trade_recovery_guard",
    ),
    "install_transport_telemetry": (
        ".transport_telemetry",
        "install_transport_telemetry",
    ),
    "DataIngestionService": (".ingestion", "DataIngestionService"),
    "kline_topic": (".ingestion", "kline_topic"),
    "liquidity_topic": (".ingestion", "liquidity_topic"),
    "live_state_topic": (".ingestion", "live_state_topic"),
    "orderbook_topic": (".ingestion", "orderbook_topic"),
    "orderflow_topic": (".ingestion", "orderflow_topic"),
    "trade_topic": (".ingestion", "trade_topic"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
