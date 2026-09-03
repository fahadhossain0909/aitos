import asyncio

import pytest

from aitos.eventbus.consumer_concurrency import (
    DEFAULT_CONSUMER_CONCURRENCY,
    install_eventbus_consumer_concurrency,
)


def test_consumer_concurrency_is_bounded(monkeypatch):
    from aitos.eventbus.consumer_concurrency import consumer_concurrency

    monkeypatch.setenv("REDIS_CONSUMER_CONCURRENCY", "3")
    assert consumer_concurrency() == 3

    monkeypatch.setenv("REDIS_CONSUMER_CONCURRENCY", "0")
    assert consumer_concurrency() == 1

    monkeypatch.setenv("REDIS_CONSUMER_CONCURRENCY", "999")
    assert consumer_concurrency() == 32

    monkeypatch.setenv("REDIS_CONSUMER_CONCURRENCY", "invalid")
    assert consumer_concurrency() == DEFAULT_CONSUMER_CONCURRENCY


class _FakeRedis:
    def __init__(self, batches):
        self.batches = {key: list(value) for key, value in batches.items()}

    async def xreadgroup(self, *, streams, **_kwargs):
        stream_key = next(iter(streams))
        batch = self.batches[stream_key]
        if batch:
            return [(stream_key, batch.pop(0))]
        await asyncio.sleep(0)
        return []


class _FakeBus:
    _known_topics = {"market.trade.BTCUSDT", "market.trade.ETHUSDT"}

    def __init__(self):
        self._redis = _FakeRedis(
            {
                "stream:market.trade.BTCUSDT": [
                    [("1-0", {"n": "1"}), ("2-0", {"n": "2"})]
                ],
                "stream:market.trade.ETHUSDT": [
                    [("1-0", {"n": "1"}), ("2-0", {"n": "2"})]
                ],
            }
        )
        self.processed = []
        self.active = 0
        self.peak = 0
        self._stop = False

    async def _ensure_group(self, *_args, **_kwargs):
        return None

    async def _reclaim_pending(self, *_args, **_kwargs):
        return []

    async def _process_message(self, stream_key, entry_id, *_args):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.processed.append((stream_key, entry_id))
        await asyncio.sleep(0.01)
        self.active -= 1


@pytest.mark.asyncio
async def test_parallelizes_independent_streams_but_preserves_order(monkeypatch):
    monkeypatch.setenv("REDIS_CONSUMER_CONCURRENCY", "2")
    install_eventbus_consumer_concurrency(_FakeBus)
    bus = _FakeBus()
    task = asyncio.create_task(
        bus._consume_loop(
            "market.trade.*", "group", "consumer", lambda _event: None
        )
    )

    for _ in range(100):
        if len(bus.processed) == 4:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert bus.peak == 2
    btc = [entry_id for stream, entry_id in bus.processed if "BTCUSDT" in stream]
    eth = [entry_id for stream, entry_id in bus.processed if "ETHUSDT" in stream]
    assert btc == ["1-0", "2-0"]
    assert eth == ["1-0", "2-0"]
