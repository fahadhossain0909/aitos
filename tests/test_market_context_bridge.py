import asyncio
from types import SimpleNamespace

import pytest

from aitos.core.contracts import Event
from aitos.eventbus.redis_bus import EventBus
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.market_context import MarketContext, handle_position_market_event


class _Provider:
    def get_context(self, symbol: str) -> MarketContext:
        return MarketContext(source=f"test:{symbol}")


class _Lifecycle:
    def __init__(self) -> None:
        self.trade = SimpleNamespace(trade_id="trade-1", symbol="BTCUSDT")
        self.updates: list[tuple[str, float, dict]] = []
        self.market_context_provider = _Provider()

    def get_open_trades(self):
        return [self.trade]

    async def update_price(self, trade_id: str, price: float, **kwargs):
        self.updates.append((trade_id, price, kwargs))


class _Redis:
    def __init__(self, event: Event) -> None:
        self._event = event
        self._sent = False

    async def ping(self):
        return True

    async def xgroup_create(self, stream, group, id="0", mkstream=True):
        return True

    async def xlen(self, stream):
        return 0

    async def xreadgroup(self, group, consumer, streams, count=100, block=100):
        if not self._sent:
            self._sent = True
            stream = next(iter(streams))
            return [(stream, [("1-0", self._event.to_wire())])]
        await asyncio.sleep(0.01)
        return []

    async def xack(self, stream, group, entry_id):
        return 1


@pytest.mark.asyncio
async def test_market_event_updates_matching_open_position():
    lifecycle = _Lifecycle()
    provider = _Provider()
    event = Event(
        topic="market.trade.BTCUSDT",
        payload={"symbol": "BTCUSDT", "price": "65000.25"},
        source_module="test",
    )

    consumed = await handle_position_market_event(lifecycle, provider, event)

    assert consumed is True
    assert len(lifecycle.updates) == 1
    trade_id, price, kwargs = lifecycle.updates[0]
    assert trade_id == "trade-1"
    assert price == 65000.25
    assert kwargs == provider.get_context("BTCUSDT").as_kwargs()


@pytest.mark.asyncio
async def test_non_market_event_is_not_consumed():
    lifecycle = _Lifecycle()
    event = Event(
        topic="trade.position_opened",
        payload={"symbol": "BTCUSDT"},
        source_module="test",
    )

    consumed = await handle_position_market_event(lifecycle, _Provider(), event)

    assert consumed is False
    assert lifecycle.updates == []


@pytest.mark.asyncio
async def test_market_event_for_other_symbol_does_not_touch_position():
    lifecycle = _Lifecycle()
    event = Event(
        topic="market.trade.ETHUSDT",
        payload={"symbol": "ETHUSDT", "price": 3000.0},
        source_module="test",
    )

    consumed = await handle_position_market_event(lifecycle, _Provider(), event)

    assert consumed is True
    assert lifecycle.updates == []


@pytest.mark.asyncio
async def test_trade_lifecycle_handler_uses_canonical_bridge():
    lifecycle = _Lifecycle()
    event = Event(
        topic="market.trade.BTCUSDT",
        payload={"symbol": "BTCUSDT", "price": 65000.25},
        source_module="test",
    )

    await TradeLifecycle.handle_event(lifecycle, event)

    assert lifecycle.updates
    assert lifecycle.updates[0][0] == "trade-1"
    assert lifecycle.updates[0][1] == 65000.25


@pytest.mark.asyncio
async def test_eventbus_binds_canonical_position_handler():
    lifecycle = _Lifecycle()
    event = Event(
        topic="market.trade.BTCUSDT",
        payload={"symbol": "BTCUSDT", "price": 65001.5},
        source_module="test",
    )
    bus = EventBus(_Redis(event))
    await bus.initialize({})

    subscription = await bus.subscribe(
        "market.trade.*",
        lifecycle.handle_event,
        group="test-position-monitor",
        start_id="$",
    )
    try:
        for _ in range(100):
            if lifecycle.updates:
                break
            await asyncio.sleep(0.01)
    finally:
        subscription.cancel()
        await bus.shutdown()

    assert lifecycle.updates
    assert lifecycle.updates[0][0] == "trade-1"
    assert lifecycle.updates[0][1] == 65001.5
