"""Runtime coordinator for the canonical market-data plane."""

from __future__ import annotations

from aitos.logging_setup import get_logger

logger = get_logger("aitos.market_data.runtime")
_RECONNECT_INITIAL_DELAY_SECONDS = 1.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0
RECONNECT_STABLE_SECONDS = 10.0
PUBLISH_RETRY_DELAY_SECONDS = 0.05
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 30.0
GATEWAY_DRAIN_WORKERS = 4
DEPTH_PRESSURE_THRESHOLD = 100
KLINE_TIMEFRAME = "1m"
KLINE_SYMBOL_LIMIT = 5
