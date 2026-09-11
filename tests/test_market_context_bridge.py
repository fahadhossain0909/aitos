from types import SimpleNamespace

import pytest

from aitos.core.contracts import Event
from aitos.trading.market_context import (
    MarketContext,
    handle_position_market_event,
)


class _Provider:
    def get_context(self, symbol: str) -> MarketContext:
        assert symbol == "BTCUSDT"
        return MarketContext(source="test")


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
    event = Event(
        topic="market.trade.BTCUSDT",
        payload={"symbol": "BTCUSDT", "price": "65000.25"},
        source_module="test",
    )

    consumed = await handle_position_market_event(lifecycle, _Provider(), event)

    assert consumed is True
    assert lifecycle.updates == [("trade-1", 65000.25, _Provider().get_context("BTCUSDT").as_kwargs())]


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
