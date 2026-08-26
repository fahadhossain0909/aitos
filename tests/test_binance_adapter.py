import asyncio
import json
from typing import List

import pytest
from aioresponses import aioresponses

from aitos.exchange.binance import (
    REST_BASE_URL,
    WS_MARKET_BASE_URL,
    WS_PUBLIC_BASE_URL,
    BinanceFuturesAdapter,
)
from tests.test_binance_parsing import (
    SAMPLE_DEPTH_REST,
    SAMPLE_KLINE_ROW,
    SAMPLE_OPEN_INTEREST,
    SAMPLE_PREMIUM_INDEX,
    SAMPLE_TRADE_REST,
)


@pytest.mark.asyncio
async def test_fetch_klines_parses_response():
    async with BinanceFuturesAdapter() as adapter:
        with aioresponses() as m:
            m.get(
                f"{REST_BASE_URL}/fapi/v1/klines?symbol=BTCUSDT&interval=1m&limit=2",
                payload=[SAMPLE_KLINE_ROW, SAMPLE_KLINE_ROW],
            )
            klines = await adapter.fetch_klines("BTCUSDT", "1m", limit=2)
    assert len(klines) == 2
    assert klines[0].symbol == "BTCUSDT"
    assert klines[0].close == 65080.25


@pytest.mark.asyncio
async def test_fetch_order_book_parses_response():
    async with BinanceFuturesAdapter() as adapter:
        with aioresponses() as m:
            m.get(
                f"{REST_BASE_URL}/fapi/v1/depth?symbol=BTCUSDT&limit=50",
                payload=SAMPLE_DEPTH_REST,
            )
            book = await adapter.fetch_order_book("BTCUSDT", limit=50)
    assert book.last_update_id == 1027024
    assert book.best_bid == 65000.00


@pytest.mark.asyncio
async def test_fetch_recent_trades_parses_response():
    async with BinanceFuturesAdapter() as adapter:
        with aioresponses() as m:
            m.get(
                f"{REST_BASE_URL}/fapi/v1/trades?symbol=BTCUSDT&limit=1",
                payload=[SAMPLE_TRADE_REST],
            )
            trades = await adapter.fetch_recent_trades("BTCUSDT", limit=1)
    assert len(trades) == 1
    assert trades[0].trade_id == 28457


@pytest.mark.asyncio
async def test_fetch_funding_rate_parses_response():
    async with BinanceFuturesAdapter() as adapter:
        with aioresponses() as m:
            m.get(
                f"{REST_BASE_URL}/fapi/v1/premiumIndex?symbol=BTCUSDT",
                payload=SAMPLE_PREMIUM_INDEX,
            )
            funding = await adapter.fetch_funding_rate("BTCUSDT")
    assert funding.mark_price == 65010.5


@pytest.mark.asyncio
async def test_fetch_open_interest_parses_response():
    async with BinanceFuturesAdapter() as adapter:
        with aioresponses() as m:
            m.get(
                f"{REST_BASE_URL}/fapi/v1/openInterest?symbol=BTCUSDT",
                payload=SAMPLE_OPEN_INTEREST,
            )
            oi = await adapter.fetch_open_interest("BTCUSDT")
    assert oi.open_interest == 45123.456


@pytest.mark.asyncio
async def test_fetch_exchange_info_parses_and_filters_by_symbol():
    sample = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "quantityPrecision": 3,
                "pricePrecision": 1,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            },
            {
                "symbol": "ETHUSDT",
                "quantityPrecision": 2,
                "pricePrecision": 2,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.01"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            },
        ]
    }
    async with BinanceFuturesAdapter() as adapter:
        with aioresponses() as m:
            m.get(f"{REST_BASE_URL}/fapi/v1/exchangeInfo", payload=sample)
            all_filters = await adapter.fetch_exchange_info()
    assert set(all_filters.keys()) == {"BTCUSDT", "ETHUSDT"}
    assert all_filters["BTCUSDT"].step_size == 0.001


@pytest.mark.asyncio
async def test_fetch_exchange_info_narrows_to_requested_symbols():
    sample = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "quantityPrecision": 3,
                "pricePrecision": 1,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            },
            {
                "symbol": "ETHUSDT",
                "quantityPrecision": 2,
                "pricePrecision": 2,
                "filters": [],
            },
        ]
    }
    async with BinanceFuturesAdapter() as adapter:
        with aioresponses() as m:
            m.get(f"{REST_BASE_URL}/fapi/v1/exchangeInfo", payload=sample)
            filtered = await adapter.fetch_exchange_info(symbols=["BTCUSDT"])
    assert set(filtered.keys()) == {"BTCUSDT"}


@pytest.mark.asyncio
async def test_fetch_before_connect_auto_connects():
    adapter = BinanceFuturesAdapter()
    with aioresponses() as m:
        m.get(
            f"{REST_BASE_URL}/fapi/v1/openInterest?symbol=BTCUSDT",
            payload=SAMPLE_OPEN_INTEREST,
        )
        oi = await adapter.fetch_open_interest("BTCUSDT")
    assert oi.open_interest == 45123.456


class FakeWebSocket:
    """Minimal async-iterable fake standing in for a ``websockets`` connection."""

    def __init__(self, messages: List[dict]):
        self._messages = messages
        self.url = None

    def __call__(self, url: str):
        self.url = url
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for msg in self._messages:
            yield json.dumps(msg)
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_stream_klines_yields_parsed_events_from_market_stream_endpoint():
    from tests.test_binance_parsing import SAMPLE_KLINE_WS

    envelope = {"stream": "btcusdt@kline_1m", "data": SAMPLE_KLINE_WS}
    fake_ws = FakeWebSocket([envelope])
    adapter = BinanceFuturesAdapter(ws_connector=fake_ws)

    received = []

    async def consume():
        async for kline in adapter.stream_klines(["BTCUSDT"], "1m"):
            received.append(kline)
            if len(received) >= 1:
                return

    await asyncio.wait_for(consume(), timeout=5)
    assert len(received) == 1
    assert received[0].symbol == "BTCUSDT"
    assert fake_ws.url == (f"{WS_MARKET_BASE_URL}?streams=btcusdt@kline_1m")


@pytest.mark.asyncio
async def test_stream_trades_yields_parsed_events_from_market_stream_endpoint():
    from tests.test_binance_parsing import SAMPLE_AGG_TRADE_WS

    envelopes = [
        {"stream": "btcusdt@aggTrade", "data": SAMPLE_AGG_TRADE_WS},
        {
            "stream": "ethusdt@aggTrade",
            "data": {**SAMPLE_AGG_TRADE_WS, "s": "ETHUSDT", "a": 1000000},
        },
    ]
    fake_ws = FakeWebSocket(envelopes)
    adapter = BinanceFuturesAdapter(ws_connector=fake_ws)

    received = []

    async def consume():
        async for trade in adapter.stream_trades(["BTCUSDT", "ETHUSDT"]):
            received.append(trade)
            if len(received) >= 2:
                return

    await asyncio.wait_for(consume(), timeout=5)
    assert [trade.symbol for trade in received] == ["BTCUSDT", "ETHUSDT"]
    assert received[0].trade_id == 999999
    assert received[1].trade_id == 1000000
    assert fake_ws.url == (
        f"{WS_MARKET_BASE_URL}?streams=btcusdt@aggTrade/ethusdt@aggTrade"
    )


@pytest.mark.asyncio
async def test_stream_order_book_uses_public_stream_endpoint():
    fake_ws = FakeWebSocket([])
    adapter = BinanceFuturesAdapter(ws_connector=fake_ws)

    producer_task = asyncio.create_task(
        adapter.stream_order_book(["BTCUSDT"], levels=20).__anext__()
    )
    try:
        await asyncio.sleep(0.1)
        assert fake_ws.url == (f"{WS_PUBLIC_BASE_URL}?streams=btcusdt@depth@100ms")
    finally:
        producer_task.cancel()
        await asyncio.gather(producer_task, return_exceptions=True)
