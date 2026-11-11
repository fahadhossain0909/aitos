"""Composition-root subscription contracts without databases or exchange IO."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aitos.app import initialize_all, shutdown_all


def components(bus):
    return SimpleNamespace(
        event_bus=bus,
        trade_lifecycle=SimpleNamespace(handle_event=AsyncMock()),
        _price_feed_subscriptions=[],
        all_modules=lambda: [],
    )


async def test_lifecycle_registers_each_price_and_cost_route_once():
    bus = SimpleNamespace(subscribe=AsyncMock(side_effect=lambda *args, **kw: Mock()))
    system = components(bus)
    await initialize_all(system)
    routes = [
        (call.args[0], call.kwargs["group"]) for call in bus.subscribe.await_args_list
    ]
    assert routes == [
        ("market.kline.*", "trade-lifecycle-prices"),
        ("market.trade.*", "trade-lifecycle-prices"),
        ("trade.cost_adjustment", "trade-lifecycle-costs"),
    ]
    assert len(system._price_feed_subscriptions) == 3
    await shutdown_all(system)
    for subscription in system._price_feed_subscriptions:
        subscription.cancel.assert_called_once()


async def test_partial_subscription_failure_keeps_handles_for_managed_shutdown():
    first = Mock()
    failure = RuntimeError("second subscription rejected")
    bus = SimpleNamespace(subscribe=AsyncMock(side_effect=[first, failure]))
    system = components(bus)
    with pytest.raises(RuntimeError) as caught:
        await initialize_all(system)
    assert caught.value is failure
    assert system._price_feed_subscriptions == [first]
    await shutdown_all(system)
    first.cancel.assert_called_once()
