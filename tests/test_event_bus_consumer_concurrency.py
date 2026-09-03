import asyncio

import pytest

from aitos.eventbus.redis_bus import EventBus


@pytest.mark.asyncio
async def test_consumer_concurrency_is_bounded(monkeypatch):
    monkeypatch.setenv("REDIS_CONSUMER_CONCURRENCY", "3")
    bus = EventBus(object())
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(_event):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1

    async def fake_process(*args, **kwargs):
        await handler(None)

    monkeypatch.setattr(bus, "_process_message", fake_process)
    semaphore = asyncio.Semaphore(3)

    async def run_one():
        async with semaphore:
            await bus._process_message("stream:test", "1-0", {}, "g", handler)

    await asyncio.gather(*(run_one() for _ in range(12)))
    assert peak == 3


@pytest.mark.asyncio
async def test_invalid_concurrency_falls_back(monkeypatch):
    monkeypatch.setenv("REDIS_CONSUMER_CONCURRENCY", "0")
    bus = EventBus(object())
    assert bus._consumer_concurrency() == 8
