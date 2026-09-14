"""Binance USDT-M Futures exchange adapter."""

from __future__ import annotations

from datetime import datetime, timezone

from aitos.logging_setup import get_logger
from aitos.market_data.endpoints import (
    BINANCE_USDM_WS_COMBINED,
    BINANCE_USDM_WS_RAW,
)

logger = get_logger("aitos.exchange.binance")
REST_BASE_URL = "https://fapi.binance.com"
WS_MARKET_BASE_URL = BINANCE_USDM_WS_COMBINED
WS_MARKET_RAW_BASE_URL = BINANCE_USDM_WS_RAW
WS_PUBLIC_BASE_URL = WS_MARKET_BASE_URL
WS_PUBLIC_RAW_BASE_URL = WS_MARKET_RAW_BASE_URL
DEFAULT_RATE_LIMIT_CAPACITY = 2000
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2000 / 60
MAX_BACKOFF_SECONDS = 60.0
INITIAL_BACKOFF_SECONDS = 1.0
ORDERBOOK_BOOTSTRAP_QUEUE_SIZE = 5000
ORDERBOOK_BOOTSTRAP_READY_TIMEOUT_SECONDS = 10.0
WS_PING_INTERVAL_SECONDS = 15.0
WS_PING_TIMEOUT_SECONDS = 10.0
WS_OPEN_TIMEOUT_SECONDS = 10.0
BINANCE_MAX_STREAMS_PER_CONNECTION = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
