from types import SimpleNamespace

import pytest

from aitos.core.contracts import Event
from aitos.trading.lifecycle import TradeLifecycle
from aitos.trading.market_context import MarketContext, handle_position_market_event


class _Provider:
    def get_context(self, symbol: str) -> MarketContext:
        return MarketContext(source=f"test:{symbol}")


class _Lifecycle:
    def __init__(self) -> None:
        self.trade = SimpleNamespace(trade_id="trade-1", symbol="BTCUSDT")
        self.updates: list[tuple[str, float, dict]] = []

    def get_open_trades(self):
        return [self.trade]

    async def update_price(self, trade_id: str, price: float, **kwargs):
        self.updates.append((trade_id, price, kwargs))


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
    lifecycle.market_context_provider = _Provider()
    event = Event(
        topic="market.trade.BTCUSDT",
        payload={"symbol": "BTCUSDT", "price": 65000.25},
        source_module="test",
    )

    await TradeLifecycle.handle_event(lifecycle, event)

    assert lifecycle.updates
    assert lifecycle.updates[0][0] == "trade-1"
    assert lifecycle.updates[0][1] == 65000.25
