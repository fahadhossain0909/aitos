from __future__ import annotations

import asyncio

import pytest

import aitos.exchange.managed_streams as managed_streams


class _FakeAdapter:
    def __init__(self, items):
        self._ws_transport = {}
        self._items = items

    async def _connect_raw_dynamic(self, **kwargs):
        for item in self._items:
            yield item


@pytest.mark.asyncio
async def test_managed_queue_reports_overflow_and_every_discard(monkeypatch):
    monkeypatch.setattr(managed_streams, "MANAGED_STREAM_QUEUE_SIZE", 2)
    adapter = _FakeAdapter(
        [({"event": index}, "btcusdt@aggTrade") for index in range(5)]
    )
    stream = managed_streams.ManagedStreamSet(adapter, "trades", ["btcusdt@aggTrade"])

    await asyncio.sleep(0)

    stats = adapter._ws_transport["managed_queues"]["trades"]
    assert stats["capacity"] == 2
    assert stats["depth"] == 1
    assert stats["max_depth"] == 2
    assert stats["overflow_events"] == 2
    assert stats["dropped_events"] == 4
    assert stats["enqueued"] == 5
    assert adapter._ws_transport["managed_queue_overflow_total"] == 2
    assert adapter._ws_transport["managed_queue_dropped_total"] == 4
    assert adapter._ws_transport["managed_queue_overflow_detected"] is True

    data, stream_name = await stream.__anext__()
    assert data == {"event": 4}
    assert stream_name == "btcusdt@aggTrade"

    stats = adapter._ws_transport["managed_queues"]["trades"]
    assert stats["dequeued"] == 1
    assert stats["depth"] == 0
    assert stats["queue_wait_max_ms"] >= 0
    await stream._queue.join()

    await stream.aclose()


@pytest.mark.asyncio
async def test_managed_queue_metrics_are_isolated_by_stream_kind(monkeypatch):
    monkeypatch.setattr(managed_streams, "MANAGED_STREAM_QUEUE_SIZE", 4)
    adapter = _FakeAdapter([({"event": 1}, "btcusdt@aggTrade")])
    trades = managed_streams.ManagedStreamSet(adapter, "trades", ["btcusdt@aggTrade"])
    books = managed_streams.ManagedStreamSet(
        adapter, "orderbook", ["btcusdt@depth@100ms"]
    )

    await asyncio.sleep(0)

    queues = adapter._ws_transport["managed_queues"]
    assert set(queues) == {"trades", "orderbook"}
    assert queues["trades"]["enqueued"] == 1
    assert queues["orderbook"]["enqueued"] == 1

    await trades.aclose()
    await books.aclose()
