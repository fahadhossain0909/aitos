from __future__ import annotations

import asyncio
import json

import pytest

from aitos.exchange.binance import BinanceFuturesAdapter
from aitos.models.market import OrderBookSnapshot


class _FakeManagedWebSocket:
    """Minimal fake supporting recv()/send(), driven by an inbox queue."""

    close_code = 1000
    close_reason = "test"

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def recv(self) -> str:
        return await self.inbox.get()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def push_trade(self, stream: str, symbol: str) -> None:
        self.inbox.put_nowait(
            json.dumps({"stream": stream, "data": {"e": "aggTrade", "s": symbol}})
        )


class _FakeManagedWebSocketContext:
    def __init__(self, ws: _FakeManagedWebSocket) -> None:
        self.ws = ws

    async def __aenter__(self) -> _FakeManagedWebSocket:
        return self.ws

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

@pytest.mark.asyncio
async def test_update_symbols_sends_incremental_subscribe_without_reconnect():
    ws = _FakeManagedWebSocket()
    connect_calls = 0

    def connector(_url: str) -> _FakeManagedWebSocketContext:
        nonlocal connect_calls
        connect_calls += 1
        return _FakeManagedWebSocketContext(ws)

    adapter = BinanceFuturesAdapter(ws_connector=connector)
    stream = adapter.stream_trades_managed(["BTCUSDT"])

    ws.push_trade("btcusdt@aggTrade", "BTCUSDT")
    payload, name = await stream.__anext__()
    assert payload["s"] == "BTCUSDT"
    assert name == "btcusdt@aggTrade"
    assert connect_calls == 1

    # Adding ETHUSDT must not reconnect -- it must send an incremental
    # SUBSCRIBE frame on the same websocket connection.
    changed = await stream.update_symbols(["BTCUSDT", "ETHUSDT"])
    assert changed is True
    # Give the pump task a chance to observe the change event and send it.
    for _ in range(50):
        if ws.sent:
            break
        await asyncio.sleep(0)
    assert connect_calls == 1, "adding a symbol must not open a new connection"
    assert ws.sent, "expected a SUBSCRIBE control frame to be sent"
    assert ws.sent[-1]["method"] == "SUBSCRIBE"
    assert ws.sent[-1]["params"] == ["ethusdt@aggTrade"]

    ws.push_trade("ethusdt@aggTrade", "ETHUSDT")
    payload, name = await stream.__anext__()
    assert payload["s"] == "ETHUSDT"
    assert connect_calls == 1

    # Removing BTCUSDT must send UNSUBSCRIBE, still no reconnect.
    await stream.update_symbols(["ETHUSDT"])
    for _ in range(50):
        if ws.sent[-1]["method"] == "UNSUBSCRIBE":
            break
        await asyncio.sleep(0)
    assert ws.sent[-1]["method"] == "UNSUBSCRIBE"
    assert ws.sent[-1]["params"] == ["btcusdt@aggTrade"]
    assert connect_calls == 1

    await stream.aclose()

@pytest.mark.asyncio
async def test_orderbook_stream_preserves_book_state_across_symbol_updates():
    ws = _FakeManagedWebSocket()

    def connector(_url: str) -> _FakeManagedWebSocketContext:
        return _FakeManagedWebSocketContext(ws)

    adapter = BinanceFuturesAdapter(ws_connector=connector)

    bootstraps: list[str] = []

    async def fake_fetch_order_book(symbol: str, limit: int = 50) -> OrderBookSnapshot:
        bootstraps.append(symbol)
        return OrderBookSnapshot(
            symbol=symbol,
            bids=((100.0, 1.0),),
            asks=((101.0, 1.0),),
            last_update_id=1,
            timestamp="2026-01-01T00:00:00+00:00",
        )

    adapter.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]

    book_stream = adapter.stream_order_book_managed(["BTCUSDT"], levels=20)

    ws.inbox.put_nowait(
        json.dumps(
            {
                "stream": "btcusdt@depth@100ms",
                "data": {
                    "U": 2,
                    "u": 2,
                    "pu": 1,
                    "b": [["100.0", "2.0"]],
                    "a": [],
                    "E": 1,
                },
            }
        )
    )
    snapshot = await book_stream.__anext__()
    assert snapshot.symbol == "BTCUSDT"
    # Managed books must begin from a REST snapshot; a diff is not a full book.
    assert bootstraps == ["BTCUSDT"]

    # Adding a second symbol must bootstrap only the new one, not BTCUSDT again.
    await book_stream.update_symbols(["BTCUSDT", "ETHUSDT"])
    ws.inbox.put_nowait(
        json.dumps(
            {
                "stream": "ethusdt@depth@100ms",
                "data": {
                    "U": 2,
                    "u": 2,
                    "pu": 1,
                    "b": [["10.0", "5.0"]],
                    "a": [],
                    "E": 1,
                },
            }
        )
    )
    snapshot = await book_stream.__anext__()
    assert snapshot.symbol == "ETHUSDT"
    # Only the newly added symbol is bootstrapped; BTCUSDT retains its book.
    assert bootstraps == ["BTCUSDT", "ETHUSDT"]

    await book_stream.aclose()

@pytest.mark.asyncio
async def test_orderbook_sequence_gap_rebootstraps_from_rest_snapshot():
    ws = _FakeManagedWebSocket()

    def connector(_url: str) -> _FakeManagedWebSocketContext:
        return _FakeManagedWebSocketContext(ws)

    adapter = BinanceFuturesAdapter(ws_connector=connector)
    bootstraps: list[int] = []

    async def fake_fetch_order_book(
        symbol: str, limit: int = 50
    ) -> OrderBookSnapshot:
        update_id = 1 if not bootstraps else 4
        bootstraps.append(update_id)
        return OrderBookSnapshot(
            symbol=symbol,
            bids=((100.0, 1.0),),
            asks=((101.0, 1.0),),
            last_update_id=update_id,
            timestamp="2026-01-01T00:00:00+00:00",
        )

    adapter.fetch_order_book = fake_fetch_order_book  # type: ignore[method-assign]
    stream = adapter.stream_order_book_managed(["BTCUSDT"], levels=20)

    ws.inbox.put_nowait(
        json.dumps(
            {
                "stream": "btcusdt@depth@100ms",
                "data": {
                    "U": 2,
                    "u": 2,
                    "pu": 1,
                    "b": [["100.0", "2.0"]],
                    "a": [],
                    "E": 1,
                },
            }
        )
    )
    first = await stream.__anext__()
    assert first.last_update_id == 2
    assert bootstraps == [1]

    # The next diff skips update 3 and breaks the chain. The implementation
    # must fetch a fresh REST snapshot and only publish after the new diff
    # bridges that snapshot.
    ws.inbox.put_nowait(
        json.dumps(
            {
                "stream": "btcusdt@depth@100ms",
                "data": {
                    "U": 5,
                    "u": 5,
                    "pu": 4,
                    "b": [["100.0", "3.0"]],
                    "a": [],
                    "E": 2,
                },
            }
        )
    )
    recovered = await stream.__anext__()
    assert recovered.last_update_id == 5
    assert bootstraps == [1, 4]

    await stream.aclose()
