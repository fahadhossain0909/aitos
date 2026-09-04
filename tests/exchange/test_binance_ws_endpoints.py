from aitos.exchange.binance import (
    WS_MARKET_BASE_URL,
    WS_MARKET_RAW_BASE_URL,
    BinanceFuturesAdapter,
)


def test_binance_futures_uses_current_combined_stream_path():
    assert WS_MARKET_BASE_URL == "wss://fstream.binance.com/stream"
    assert WS_MARKET_RAW_BASE_URL == "wss://fstream.binance.com/ws"

    adapter = BinanceFuturesAdapter()
    assert adapter._ws_base_url(["btcusdt@aggTrade"]) == WS_MARKET_BASE_URL
    assert adapter._ws_base_url(["btcusdt@depth@100ms"]) == WS_MARKET_BASE_URL
