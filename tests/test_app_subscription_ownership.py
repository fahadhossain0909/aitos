"""Composition-root subscription contracts without databases or exchange IO."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aitos.app import initialize_all, shutdown_all


def components(bus):
    return SimpleNamespace(
        event_bus=bus,
        trade_lifecycle=SimpleNamespace(handle_event=AsyncMock()),
        _price_feed_subscriptions=[],
        all_modules=list,
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



async def test_governance_drift_scheduler_is_owned_by_application_lifecycle():
    bus = SimpleNamespace(subscribe=AsyncMock(side_effect=lambda *args, **kw: Mock()))
    system = components(bus)
    provider_calls = []

    class FakeDriftScheduler:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = False

        async def run_forever(self, stop_event, batch_provider):
            provider_calls.append(batch_provider)
            self.started.set()
            await stop_event.wait()
            self.stopped = True

    scheduler = FakeDriftScheduler()

    def provider():
        return {}, {}
    system.governance_drift_scheduler = scheduler
    system.governance_drift_batch_provider = provider
    system._governance_drift_task = None
    system._governance_drift_stop_event = None

    await initialize_all(system)
    await asyncio.wait_for(scheduler.started.wait(), timeout=1.0)
    assert provider_calls == [provider]
    assert system._governance_drift_task is not None

    await shutdown_all(system)
    assert scheduler.stopped is True
    assert system._governance_drift_task is None
    assert system._governance_drift_stop_event is None
