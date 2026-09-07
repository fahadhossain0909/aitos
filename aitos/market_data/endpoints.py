"""Current public market-data WebSocket endpoints.

Keep exchange endpoint selection in one place so adapters cannot silently drift
back to deprecated paths. Regional OKX routing remains opt-in because the
correct regional service depends on the account's applicable jurisdiction.
"""

from __future__ import annotations

import os

BINANCE_USDM_WS_BASE = "wss://fstream.binance.com"
# Binance's current USDⓈ-M WebSocket Streams SDK connects to the market
# routing path and then sends SUBSCRIBE requests over that connection.
BINANCE_USDM_WS_COMBINED = f"{BINANCE_USDM_WS_BASE}/market/stream"
# Kept for compatibility with callers/tests that use the raw-stream constant.
BINANCE_USDM_WS_RAW = f"{BINANCE_USDM_WS_BASE}/market/ws"
# Legacy public-route names are retained as aliases. All USDⓈ-M public market
# streams are intentionally routed through the validated market path.
BINANCE_USDM_WS_PUBLIC_COMBINED = BINANCE_USDM_WS_COMBINED
BINANCE_USDM_WS_PUBLIC_RAW = BINANCE_USDM_WS_RAW

BYBIT_LINEAR_WS = os.getenv(
    "BYBIT_LINEAR_WS_URL", "wss://stream.bybit.com/v5/public/linear"
)

OKX_PUBLIC_WS = os.getenv("OKX_WS_PUBLIC_URL", "wss://ws.okx.com:8443/ws/v5/public")

# Exchange lifecycle/heartbeat contracts documented by the venues.
BINANCE_WS_MAX_LIFETIME_SECONDS = 23 * 60 * 60 + 50 * 60
BINANCE_USDM_WS_MAX_LIFETIME_SECONDS = BINANCE_WS_MAX_LIFETIME_SECONDS
BYBIT_WS_HEARTBEAT_INTERVAL_SECONDS = 20.0
OKX_WS_HEARTBEAT_INTERVAL_SECONDS = 20.0
