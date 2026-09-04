from datetime import timezone

from aitos.market_data.bybit_adapter import BybitCanonicalMarketDataAdapter
from aitos.market_data.contracts import MarketEventType, MarketSource
from aitos.market_data.okx_adapter import OKXCanonicalMarketDataAdapter


def test_bybit_trade_normalizes_identity_and_sequence():
    adapter = BybitCanonicalMarketDataAdapter()
    event = adapter._parse_trade(
        {
            "topic": "publicTrade.BTCUSDT",
            "ts": 1770000000123,
            "data": [
                {
                    "T": 1770000000000,
                    "s": "BTCUSDT",
                    "S": "Buy",
                    "v": "0.5",
                    "p": "90000.1",
                    "i": "12345",
                    "seq": 98765,
                },
            ],
        }
    )
    assert event is not None
    assert event.venue == "bybit"
    assert event.market_type == "usd_m_futures"
    assert event.symbol == "BTCUSDT"
    assert event.sequence == 98765
    assert event.source is MarketSource.WEBSOCKET
    assert event.event_time.tzinfo == timezone.utc


def test_bybit_orderbook_snapshot_then_delta_is_emitted_as_snapshot():
    adapter = BybitCanonicalMarketDataAdapter()
    first = adapter._parse_book(
        {
            "topic": "orderbook.50.BTCUSDT",
            "type": "snapshot",
            "ts": 1770000000000,
            "data": {"s": "BTCUSDT", "u": 100, "b": [["99", "2"]], "a": [["101", "3"]]},
        }
    )
    second = adapter._parse_book(
        {
            "topic": "orderbook.50.BTCUSDT",
            "type": "delta",
            "ts": 1770000000100,
            "data": {"s": "BTCUSDT", "u": 101, "b": [["99", "0"], ["98", "4"]], "a": []},
        }
    )
    assert first is not None and second is not None
    assert first.event_type is MarketEventType.BOOK_SNAPSHOT
    assert second.event_type is MarketEventType.BOOK_SNAPSHOT
    assert second.payload["bids"] == [{"price": 98.0, "quantity": 4.0}]
    assert second.payload["asks"] == [{"price": 101.0, "quantity": 3.0}]


def test_okx_symbol_mapping_is_canonical():
    adapter = OKXCanonicalMarketDataAdapter()
    assert adapter._instrument("BTCUSDT") == "BTC-USDT-SWAP"
    assert adapter._symbol("BTC-USDT-SWAP") == "BTCUSDT"


def test_okx_trade_and_book_are_canonical():
    adapter = OKXCanonicalMarketDataAdapter()
    trade = adapter._parse_trade(
        {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP", "tradeId": "123", "px": "90000", "sz": "1", "side": "buy", "ts": "1770000000000"}],
        }
    )
    book = adapter._parse_book(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP", "action": "snapshot", "seqId": "7", "ts": "1770000000000", "bids": [["89999", "2", "1"]], "asks": [["90001", "3", "1"]]}],
        }
    )
    assert trade is not None and book is not None
    assert trade.event_type is MarketEventType.TRADE
    assert trade.symbol == "BTCUSDT"
    assert trade.instrument_id == "okx:perpetual:BTC-USDT-SWAP"
    assert book.event_type is MarketEventType.BOOK_SNAPSHOT
    assert book.symbol == "BTCUSDT"
    assert book.payload["bids"] == [{"price": 89999.0, "quantity": 2.0}]
