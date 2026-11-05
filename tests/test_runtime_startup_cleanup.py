"""Failure-injection tests for trading-entrypoint startup resource ownership."""

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_live_main_closes_event_bus_and_redis_if_clickhouse_startup_fails(
    monkeypatch,
):
    import aitos.eventbus.factory as eventbus_factory
    import run_live_trading as runner

    class FakeRedis:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeEventBus:
        def __init__(self):
            self.closed = False

        async def initialize(self, config):
            return None

        async def shutdown(self, grace_period_seconds=30.0):
            self.closed = True

    redis = FakeRedis()
    event_bus = FakeEventBus()
    settings = SimpleNamespace(
        tuning=SimpleNamespace(mode="live"),
        log_level="INFO",
    )

    async def connect_redis(*args, **kwargs):
        return redis

    async def fail_clickhouse(*args, **kwargs):
        raise RuntimeError("ClickHouse startup failed")

    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    monkeypatch.setattr(runner, "configure_logging", lambda level: None)
    monkeypatch.setattr(runner, "connect_redis_with_retry", connect_redis)
    monkeypatch.setattr(
        eventbus_factory,
        "create_event_bus",
        lambda *args, **kwargs: event_bus,
    )
    monkeypatch.setattr(runner, "connect_clickhouse_repositories", fail_clickhouse)

    with pytest.raises(RuntimeError, match="ClickHouse startup failed"):
        await runner.main()

    assert event_bus.closed is True
    assert redis.closed is True
