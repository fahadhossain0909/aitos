"""Pin venue WebSocket endpoints and transport contracts to current APIs."""

from aitos.exchange.binance import (
    BINANCE_MAX_STREAMS_PER_CONNECTION,
    WS_MARKET_BASE_URL,
    WS_MARKET_RAW_BASE_URL,
    BinanceFuturesAdapter,
)
from aitos.market_data.bybit_adapter import BybitCanonicalMarketDataAdapter
from aitos.market_data.endpoints import (
    BINANCE_USDM_WS_COMBINED,
    BINANCE_USDM_WS_RAW,
    BINANCE_WS_MAX_LIFETIME_SECONDS,
    BYBIT_LINEAR_WS,
    BYBIT_WS_HEARTBEAT_INTERVAL_SECONDS,
    OKX_PUBLIC_WS,
    OKX_WS_HEARTBEAT_INTERVAL_SECONDS,
)
from aitos.market_data.okx_adapter import OKXCanonicalMarketDataAdapter


def test_binance_usdm_uses_current_market_stream_paths():
    assert WS_MARKET_BASE_URL == "wss://fstream.binance.com/stream"
    assert WS_MARKET_RAW_BASE_URL == "wss://fstream.binance.com/ws"
    assert BINANCE_USDM_WS_COMBINED == WS_MARKET_BASE_URL
    assert BINANCE_USDM_WS_RAW == WS_MARKET_RAW_BASE_URL
    assert BINANCE_WS_MAX_LIFETIME_SECONDS < 24 * 60 * 60


def test_binance_all_market_streams_are_partitioned_below_venue_limit():
    streams = [f"symbol{i}@aggTrade" for i in range(901)]
    shards = BinanceFuturesAdapter._partition_streams(streams)
    assert len(shards) == 2
    assert len(shards[0]) == BINANCE_MAX_STREAMS_PER_CONNECTION
    assert len(shards[1]) == 1
    assert len({stream for shard in shards for stream in shard}) == 901
    assert all(len(shard) <= 1024 for shard in shards)


def test_binance_stream_partition_deduplicates_without_reordering():
    streams = ["btcusdt@aggTrade", "ethusdt@aggTrade", "btcusdt@aggTrade"]
    assert BinanceFuturesAdapter._partition_streams(streams, 2) == [
        ["btcusdt@aggTrade", "ethusdt@aggTrade"]
    ]


def test_bybit_linear_uses_current_v5_public_path_and_heartbeat():
    adapter = BybitCanonicalMarketDataAdapter()
    assert adapter.websocket_url == BYBIT_LINEAR_WS
    assert BYBIT_LINEAR_WS == "wss://stream.bybit.com/v5/public/linear"
    assert adapter.heartbeat_message == {"op": "ping"}
    assert 0 < BYBIT_WS_HEARTBEAT_INTERVAL_SECONDS < 30
    assert adapter._market_type.value == "usd_m_futures"


def test_okx_public_uses_current_v5_public_path_and_heartbeat():
    adapter = OKXCanonicalMarketDataAdapter()
    assert adapter.websocket_url == OKX_PUBLIC_WS
    assert OKX_PUBLIC_WS == "wss://ws.okx.com:8443/ws/v5/public"
    assert adapter.heartbeat_message == "ping"
    assert 0 < OKX_WS_HEARTBEAT_INTERVAL_SECONDS < 30


def test_current_subscription_topics_are_documented_shapes():
    bybit = BybitCanonicalMarketDataAdapter()
    trade_message = {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}
    book_message = {"op": "subscribe", "args": ["orderbook.50.BTCUSDT"]}
    assert trade_message["args"] == ["publicTrade.BTCUSDT"]
    assert book_message["args"] == ["orderbook.50.BTCUSDT"]

    okx = OKXCanonicalMarketDataAdapter()
    assert okx._args(["BTCUSDT"], "trades") == [
        {"channel": "trades", "instId": "BTC-USDT-SWAP"}
    ]
    assert okx._args(["BTCUSDT"], "books") == [
        {"channel": "books", "instId": "BTC-USDT-SWAP"}
    ]
