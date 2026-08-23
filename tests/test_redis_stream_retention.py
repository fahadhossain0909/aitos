from unittest.mock import AsyncMock

import pytest

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus, _stream_maxlen


@pytest.mark.asyncio
async def test_high_volume_market_streams_are_bounded() -> None:
    redis = AsyncMock()
    bus = EventBus(redis)
    await bus.initialize({})

    await bus.publish(Event(topic="market.orderbook.BTCUSDT", payload={"bid": 1}))
    await bus.publish(Event(topic="market.liquidity.BTCUSDT", payload={"depth": 1}))
    await bus.publish(Event(topic="market.live_state.BTCUSDT", payload={"mid": 1}))

    calls = redis.xadd.await_args_list
    assert calls[0].kwargs["maxlen"] == 25_000
    assert calls[0].kwargs["approximate"] is True
    assert calls[1].kwargs["maxlen"] == 100_000
    assert calls[2].kwargs["maxlen"] == 25_000


@pytest.mark.asyncio
async def test_protected_streams_remain_unbounded() -> None:
    redis = AsyncMock()
    bus = EventBus(redis)
    await bus.initialize({})

    await bus.publish(
        Event(topic="trade.position_opened", payload={"symbol": "BTCUSDT"})
    )
    await bus.publish(
        Event(topic="journal.decision_recorded", payload={"decision": "hold"})
    )

    for call in redis.xadd.await_args_list:
        assert "maxlen" not in call.kwargs
        assert "approximate" not in call.kwargs


def test_retention_can_be_overridden_per_stream_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_STREAM_MAXLEN_MARKET_ORDERBOOK", "5000")
    monkeypatch.setenv("REDIS_STREAM_MAXLEN_MARKET_LIQUIDITY", "20000")
    monkeypatch.setenv("REDIS_STREAM_MAXLEN_MARKET_LIVE_STATE", "5000")

    assert _stream_maxlen("market.orderbook.BTCUSDT") == 5000
    assert _stream_maxlen("market.liquidity.BTCUSDT") == 20000
    assert _stream_maxlen("market.live_state.BTCUSDT") == 5000
    assert _stream_maxlen("trade.position_opened") is None
