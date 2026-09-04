from datetime import timezone

from aitos.market_data.bybit_adapter import BybitCanonicalMarketDataAdapter
from aitos.market_data.contracts import MarketEventType, MarketSource
from aitos.market_data.okx_adapter import OKXCanonicalMarketDataAdapter


def test_bybit_trade_normalizes_identity_and_sequence():
    adapter = BybitCanonicalMarketDataAdapter()
    events = adapter._parse_trade(
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
    assert events is not None and len(events) == 1
    event = events[0]
    assert event.venue == "bybit"
    assert event.market_type == "usd_m_futures"
    assert event.symbol == "BTCUSDT"
    assert event.sequence == 98765
    assert event.source is MarketSource.WEBSOCKET
    assert event.event_time.tzinfo == timezone.utc


def test_bybit_batched_trades_are_all_emitted():
    adapter = BybitCanonicalMarketDataAdapter()
    events = adapter._parse_trade(
        {
            "topic": "publicTrade.BTCUSDT",
            "ts": 1770000000123,
            "data": [
                {"T": 1770000000000, "s": "BTCUSDT", "S": "Buy", "v": "0.5", "p": "90000.1", "i": "1", "seq": 10},
                {"T": 1770000000001, "s": "BTCUSDT", "S": "Sell", "v": "0.7", "p": "90000.2", "i": "2", "seq": 11},
            ],
        }
    )
    assert events is not None
    assert [event.payload["trade_id"] for event in events] == ["1", "2"]
    assert [event.sequence for event in events] == [10, 11]


def test_bybit_orderbook_snapshot_then_delta_is_reconstructed():
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
    assert second.event_type is MarketEventType.BOOK_DELTA
    assert second.payload["bids"] == [{"price": 98.0, "quantity": 4.0}]


def test_okx_symbol_mapping_is_canonical():
    adapter = OKXCanonicalMarketDataAdapter()
    assert adapter._instrument("BTCUSDT") == "BTC-USDT-SWAP"
    assert adapter._symbol("BTC-USDT-SWAP") == "BTCUSDT"


def test_okx_trade_and_book_are_canonical():
    adapter = OKXCanonicalMarketDataAdapter()
    trades = adapter._parse_trade(
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
    assert trades is not None and len(trades) == 1 and book is not None
    trade = trades[0]
    assert trade.event_type is MarketEventType.TRADE
    assert trade.symbol == "BTCUSDT"
    assert trade.instrument_id == "okx:perpetual:BTC-USDT-SWAP"
    assert book.event_type is MarketEventType.BOOK_SNAPSHOT
    assert book.symbol == "BTCUSDT"
    assert book.payload["bids"] == [{"price": 89999.0, "quantity": 2.0}]


def test_okx_batched_trades_are_all_emitted():
    adapter = OKXCanonicalMarketDataAdapter()
    events = adapter._parse_trade(
        {
            "arg": {"channel": "trades"},
            "data": [
                {"instId": "BTC-USDT-SWAP", "tradeId": "21", "ts": "1770000000000", "px": "100", "sz": "1", "side": "buy"},
                {"instId": "BTC-USDT-SWAP", "tradeId": "22", "ts": "1770000000001", "px": "101", "sz": "2", "side": "sell"},
            ],
        }
    )
    assert events is not None
    assert [event.payload["trade_id"] for event in events] == ["21", "22"]
