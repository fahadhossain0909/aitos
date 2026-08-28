from aitos.exchange.parsing import (
    parse_agg_trade_ws,
    parse_depth_ws,
    parse_funding_rate_rest,
    parse_kline_rest,
    parse_kline_ws,
    parse_open_interest_rest,
    parse_order_book_rest,
    parse_trade_rest,
    parse_trade_ws,
)
from aitos.models.market import TradeSide

# Sample payloads shaped exactly like Binance USDT-M Futures API responses.

SAMPLE_KLINE_ROW = [
    1718000000000,
    "65000.10",
    "65100.00",
    "64950.50",
    "65080.25",
    "123.456",
    1718000059999,
    "8031234.56",
    4521,
    "70.111",
    "4560000.12",
    "0",
]

SAMPLE_DEPTH_REST = {
    "lastUpdateId": 1027024,
    "E": 1718000000000,
    "T": 1718000000000,
    "bids": [["65000.00", "1.5"], ["64999.50", "2.0"]],
    "asks": [["65000.50", "1.0"], ["65001.00", "0.8"]],
}

SAMPLE_TRADE_REST = {
    "id": 28457,
    "price": "65000.00",
    "qty": "0.5",
    "quoteQty": "32500.0",
    "time": 1718000000000,
    "isBuyerMaker": True,
}

SAMPLE_PREMIUM_INDEX = {
    "symbol": "BTCUSDT",
    "markPrice": "65010.5",
    "indexPrice": "65005.0",
    "estimatedSettlePrice": "65008.0",
    "lastFundingRate": "0.00010000",
    "nextFundingTime": 1718000400000,
    "interestRate": "0.00010000",
    "time": 1718000000000,
}

SAMPLE_OPEN_INTEREST = {
    "openInterest": "45123.456",
    "symbol": "BTCUSDT",
    "time": 1718000000000,
}

SAMPLE_KLINE_WS = {
    "e": "kline",
    "E": 1718000000000,
    "s": "BTCUSDT",
    "k": {
        "t": 1718000000000,
        "T": 1718000059999,
        "s": "BTCUSDT",
        "i": "1m",
        "o": "65000.10",
        "c": "65080.25",
        "h": "65100.00",
        "l": "64950.50",
        "v": "123.456",
        "n": 4521,
        "x": False,
        "q": "8031234.56",
        "V": "70.111",
        "Q": "4560000.12",
    },
}

SAMPLE_AGG_TRADE_WS = {
    "e": "aggTrade",
    "E": 1718000000000,
    "s": "BTCUSDT",
    "a": 999999,
    "p": "65000.00",
    "q": "0.25",
    "f": 100,
    "l": 105,
    "T": 1718000000000,
    "m": False,
}

SAMPLE_TRADE_WS = {
    "e": "trade",
    "E": 1718000000000,
    "s": "BTCUSDT",
    "t": 1234567,
    "p": "65001.00",
    "q": "0.125",
    "T": 1718000000000,
    "m": True,
}

SAMPLE_DEPTH_WS = {
    "e": "depthUpdate",
    "E": 1718000000000,
    "T": 1718000000000,
    "s": "BTCUSDT",
    "U": 100,
    "u": 110,
    "b": [["65000.00", "1.5"]],
    "a": [["65000.50", "1.0"]],
}


def test_parse_kline_rest():
    kline = parse_kline_rest(SAMPLE_KLINE_ROW, symbol="BTCUSDT", timeframe="1m")
    assert kline.symbol == "BTCUSDT"
    assert kline.open == 65000.10
    assert kline.close == 65080.25
    assert kline.trades_count == 4521
    assert kline.is_closed is True


def test_parse_order_book_rest():
    book = parse_order_book_rest(SAMPLE_DEPTH_REST, symbol="BTCUSDT")
    assert book.last_update_id == 1027024
    assert book.best_bid == 65000.00
    assert book.best_ask == 65000.50
    assert len(book.bids) == 2 and len(book.asks) == 2


def test_parse_trade_rest_buyer_maker_is_sell_side():
    trade = parse_trade_rest(SAMPLE_TRADE_REST, symbol="BTCUSDT")
    assert trade.side == TradeSide.SELL
    assert trade.trade_id == 28457


def test_parse_funding_rate_rest():
    funding = parse_funding_rate_rest(SAMPLE_PREMIUM_INDEX)
    assert funding.symbol == "BTCUSDT"
    assert funding.funding_rate == 0.0001
    assert funding.mark_price == 65010.5


def test_parse_open_interest_rest():
    oi = parse_open_interest_rest(SAMPLE_OPEN_INTEREST)
    assert oi.open_interest == 45123.456


def test_parse_kline_ws():
    kline = parse_kline_ws(SAMPLE_KLINE_WS)
    assert kline.symbol == "BTCUSDT"
    assert kline.timeframe == "1m"
    assert kline.is_closed is False


def test_parse_agg_trade_ws_buyer_not_maker_is_buy_side():
    trade = parse_agg_trade_ws(SAMPLE_AGG_TRADE_WS)
    assert trade.side == TradeSide.BUY
    # ``a`` is the aggregate-trade ID. ``l`` is the last raw trade ID and is
    # the canonical cursor shared with @trade and REST /fapi/v1/trades.
    assert trade.trade_id == 105


def test_parse_agg_trade_and_raw_trade_share_dedup_id_namespace():
    aggregate = parse_agg_trade_ws(SAMPLE_AGG_TRADE_WS)
    raw_payload = dict(SAMPLE_TRADE_WS, t=106)
    raw = parse_trade_ws(raw_payload)
    assert aggregate.trade_id == 105
    assert raw.trade_id == 106
    assert raw.trade_id > aggregate.trade_id


def test_parse_raw_trade_ws_buyer_maker_is_sell_side():
    trade = parse_trade_ws(SAMPLE_TRADE_WS)
    assert trade.symbol == "BTCUSDT"
    assert trade.side == TradeSide.SELL
    assert trade.trade_id == 1234567
    assert trade.timestamp.isoformat() == "2024-06-10T06:13:20+00:00"


def test_parse_depth_ws():
    book = parse_depth_ws(SAMPLE_DEPTH_WS, symbol="BTCUSDT")
    assert book.symbol == "BTCUSDT"
    assert book.bids == ((65000.00, 1.5),)
    assert book.asks == ((65000.50, 1.0),)
