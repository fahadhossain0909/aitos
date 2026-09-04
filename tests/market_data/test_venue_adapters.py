from aitos.market_data import (
    BybitCanonicalMarketDataAdapter,
    CanonicalMarketDataAdapter,
    MarketEventType,
    MarketType,
    OKXCanonicalMarketDataAdapter,
    Venue,
)


def test_bybit_adapter_conforms_and_parses_trade():
    adapter = BybitCanonicalMarketDataAdapter()
    assert isinstance(adapter, CanonicalMarketDataAdapter)
    event = adapter._parse_trade(
        {
            "topic": "publicTrade.BTCUSDT",
            "ts": 1000,
            "data": [
                {
                    "T": "2000",
                    "s": "BTCUSDT",
                    "p": "100.5",
                    "v": "0.25",
                    "S": "Buy",
                    "i": "42",
                }
            ],
        }
    )
    assert event is not None
    assert event.venue == Venue.BYBIT
    assert event.market_type == MarketType.USD_M_FUTURES
    assert event.event_type is MarketEventType.TRADE
    assert event.sequence == 42
    assert event.payload["price"] == 100.5


def test_bybit_book_parser_accepts_snapshot_and_delta():
    adapter = BybitCanonicalMarketDataAdapter()
    message = {
        "topic": "orderbook.50.BTCUSDT",
        "type": "snapshot",
        "ts": 1000,
        "data": {
            "s": "BTCUSDT",
            "u": 99,
            "b": [["100", "2"]],
            "a": [["101", "3"]],
        },
    }
    event = adapter._parse_book(message)
    assert event is not None
    assert event.event_type is MarketEventType.BOOK_SNAPSHOT
    assert event.sequence == 99
    assert event.payload["bids"][0]["quantity"] == 2.0


def test_okx_adapter_normalizes_usdt_symbol_and_parses_trade():
    adapter = OKXCanonicalMarketDataAdapter()
    assert isinstance(adapter, CanonicalMarketDataAdapter)
    assert adapter._instrument("BTCUSDT") == "BTC-USDT-SWAP"
    event = adapter._parse_trade(
        {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "tradeId": "77",
                    "px": "100.5",
                    "sz": "1.5",
                    "side": "buy",
                    "ts": "2000",
                }
            ],
        }
    )
    assert event is not None
    assert event.venue == Venue.OKX
    assert event.event_type is MarketEventType.TRADE
    assert event.sequence == 77
    assert event.payload["quantity"] == 1.5


def test_okx_book_parser_maps_snapshot():
    adapter = OKXCanonicalMarketDataAdapter()
    event = adapter._parse_book(
        {
            "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ts": "2000",
                    "seqId": "88",
                    "action": "snapshot",
                    "bids": [["100", "2", "0", "1"]],
                    "asks": [["101", "3", "0", "1"]],
                }
            ],
        }
    )
    assert event is not None
    assert event.event_type is MarketEventType.BOOK_SNAPSHOT
    assert event.sequence == 88
    assert event.payload["asks"][0]["price"] == 101.0
