"""Pin venue WebSocket endpoints to the currently documented public APIs."""

from aitos.exchange.binance import WS_MARKET_BASE_URL, WS_MARKET_RAW_BASE_URL
from aitos.market_data.bybit_adapter import BybitCanonicalMarketDataAdapter
from aitos.market_data.okx_adapter import OKXCanonicalMarketDataAdapter


def test_binance_usdm_uses_current_market_stream_paths():
    assert WS_MARKET_BASE_URL == "wss://fstream.binance.com/stream"
    assert WS_MARKET_RAW_BASE_URL == "wss://fstream.binance.com/ws"


def test_bybit_linear_uses_current_v5_public_path():
    assert (
        BybitCanonicalMarketDataAdapter.websocket_url
        == "wss://stream.bybit.com/v5/public/linear"
    )


def test_okx_public_uses_current_v5_public_path():
    assert (
        OKXCanonicalMarketDataAdapter.websocket_url
        == "wss://ws.okx.com:8443/ws/v5/public"
    )
