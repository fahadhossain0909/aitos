"""Binance USDT-M Futures exchange adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

import aiohttp

from aitos.exchange.base import ExchangeAdapter
from aitos.exchange.orderbook import LocalOrderBook, OrderBookSequenceError
from aitos.exchange.parsing import (
    parse_agg_trade_ws,
    parse_depth_diff_ws,
    parse_funding_rate_rest,
    parse_kline_rest,
    parse_kline_ws,
    parse_open_interest_rest,
    parse_order_book_rest,
    parse_trade_rest,
)
from aitos.exchange.rate_limiter import TokenBucketRateLimiter
from aitos.exchange.symbol_filters import SymbolFilters, parse_exchange_info
from aitos.logging_setup import get_logger
from aitos.market_data.endpoints import (
    BINANCE_USDM_WS_COMBINED,
    BINANCE_USDM_WS_MAX_LIFETIME_SECONDS,
    BINANCE_USDM_WS_RAW,
)
from aitos.models.market import (
    FundingRate,
    Kline,
    OpenInterest,
    OrderBookSnapshot,
    TradeTick,
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
