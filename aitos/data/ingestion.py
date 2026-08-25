"""DataIngestionService — the glue between exchange streams and AITOS."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventPriority,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.data.repository import MarketDataRepository
from aitos.eventbus.redis_bus import EventBus
from aitos.exchange.base import ExchangeAdapter
from aitos.intelligence.live_state import LiveMarketStateStore
from aitos.logging_setup import get_logger
from aitos.models.market import Kline, OrderBookSnapshot, TradeTick

logger = get_logger("aitos.data.ingestion")

TRADE_STREAM_IDLE_TIMEOUT_SECONDS = 30.0
TRADE_STREAM_RESTART_DELAY_SECONDS = 1.0
TRADE_STREAM_QUEUE_SIZE = 1000
TRADE_FALLBACK_LIMIT = 100
ORDERBOOK_PERSIST_INTERVAL_SECONDS = 1.0
# Shared backoff when a long-lived market stream task crashes.
# Trade stream already restarts; kline/orderbook must do the same so a single
# transient WS/bootstrap failure does not leave data-ingestion permanently dead.
STREAM_RESTART_DELAY_SECONDS = 1.0
