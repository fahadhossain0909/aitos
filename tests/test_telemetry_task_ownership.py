"""Telemetry tasks belong to initialized modules, not constructors."""

import gc
import warnings

import pytest

from aitos.forensics.runtime_contention_telemetry import _install_eventbus
from aitos.forensics.safe_market_data_telemetry import install


def bus_type(order="safe-first"):
    class Bus:
        def __init__(self):
            self.initializations = 0
            self.shutdowns = 0
            self.fail_initialize = False
            self.fail_shutdown = False

        async def initialize(self, config):
            if self.fail_initialize:
                raise RuntimeError("injected startup failure")
            self.initializations += 1

        async def publish(self, event):
            return None

        async def subscribe(self, *args, **kwargs):
            return None

        async def health_check(self):
            return None

        async def shutdown(self, *args, **kwargs):
            self.shutdowns += 1
            if self.fail_shutdown:
                raise RuntimeError("injected shutdown failure")

    if order == "safe-first":
        install(Bus)
        _install_eventbus(Bus)
    else:
        _install_eventbus(Bus)
        install(Bus)
    return Bus


def test_sync_construction_does_not_create_unawaited_coroutine():
    Bus = bus_type()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        instance = Bus()
        gc.collect()
    assert instance._safe_market_watchdog is None
    assert not [
        warning for warning in captured if "never awaited" in str(warning.message)
    ]


@pytest.mark.parametrize("order", ["safe-first", "contention-first"])
async def test_tasks_start_after_initialization_stop_and_restart(order):
    instance = bus_type(order)()
    assert instance._safe_market_watchdog is None
    assert instance._contention_watchdog_task is None
    await instance.initialize({})
    first = (instance._safe_market_watchdog, instance._contention_watchdog_task)
    assert all(task is not None and not task.done() for task in first)
    await instance.initialize({})
    assert first == (instance._safe_market_watchdog, instance._contention_watchdog_task)
    await instance.shutdown()
    assert all(task.done() for task in first)
    assert instance._safe_market_watchdog is None
    assert instance._contention_watchdog_task is None
    await instance.initialize({})
    second = (instance._safe_market_watchdog, instance._contention_watchdog_task)
    assert all(task is not old for task, old in zip(second, first))
    await instance.shutdown()
    await instance.shutdown()
    assert all(task.done() for task in second)


@pytest.mark.parametrize("order", ["safe-first", "contention-first"])
async def test_failed_startup_never_spawns_watchdogs(order):
    instance = bus_type(order)()
    instance.fail_initialize = True
    with pytest.raises(RuntimeError, match="startup failure"):
        await instance.initialize({})
    assert instance._safe_market_watchdog is None
    assert instance._contention_watchdog_task is None


@pytest.mark.parametrize("order", ["safe-first", "contention-first"])
async def test_watchdogs_stop_even_if_inner_shutdown_raises(order):
    instance = bus_type(order)()
    await instance.initialize({})
    tasks = (instance._safe_market_watchdog, instance._contention_watchdog_task)
    instance.fail_shutdown = True
    with pytest.raises(RuntimeError, match="shutdown failure"):
        await instance.shutdown()
    assert all(task.done() for task in tasks)
