import asyncio

import pytest

from aitos.core.contracts import Event
from aitos.knowledge_graph.writer import (CLOSE_TRADE_QUERY, CORRELATION_QUERY,
                                          CREATE_TRADE_QUERY,
                                          LINK_MISTAKE_QUERY,
                                          KnowledgeGraphWriter)


class FakeSession:
    def __init__(self, calls):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def run(self, query, **params):
        self._calls.append((query, params))


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.closed = False

    def session(self):
        return FakeSession(self.calls)

    async def close(self):
        self.closed = True


async def _wait_for(predicate, timeout=3.0, interval=0.05):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


@pytest.mark.asyncio
async def test_position_opened_creates_trade_symbol_strategy_nodes(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    payload = {
        "trade_id": "t1",
        "symbol": "BTCUSDT",
        "strategy_id": "scanner-v1",
        "side": "LONG",
        "entry_price": 100.0,
        "regime": "trending",
        "state": "position_opened",
        "entry_time": "2026-07-11T00:00:00Z",
    }
    await event_bus.publish(
        Event(topic="trade.position_opened", payload=payload, source_module="test")
    )

    assert await _wait_for(lambda: len(driver.calls) == 1)
    query, params = driver.calls[0]
    assert query == CREATE_TRADE_QUERY
    assert params["trade_id"] == "t1"
    assert params["symbol"] == "BTCUSDT"
    assert params["strategy_id"] == "scanner-v1"
    assert params["regime"] == "trending"

    await writer.shutdown()
    assert driver.closed is True


@pytest.mark.asyncio
async def test_position_closed_updates_trade_node(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    payload = {
        "trade_id": "t1",
        "pnl": 150.0,
        "pnl_percent": 15.0,
        "exit_price": 104.0,
        "exit_reason": "tp_triggered",
        "exit_time": "x",
        "state": "position_closed",
    }
    await event_bus.publish(
        Event(topic="trade.position_closed", payload=payload, source_module="test")
    )

    assert await _wait_for(lambda: len(driver.calls) == 1)
    query, params = driver.calls[0]
    assert query == CLOSE_TRADE_QUERY
    assert params["pnl"] == 150.0
    assert params["trade_id"] == "t1"

    await writer.shutdown()


@pytest.mark.asyncio
async def test_mistake_recorded_links_to_trade(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    payload = {"trade_id": "t1", "mistakes": ["entered too early"], "created_at": "x"}
    await event_bus.publish(
        Event(topic="journal.mistake_recorded", payload=payload, source_module="test")
    )

    assert await _wait_for(lambda: len(driver.calls) == 1)
    query, params = driver.calls[0]
    assert query == LINK_MISTAKE_QUERY
    assert params["mistake_text"] == "entered too early"

    await writer.shutdown()


@pytest.mark.asyncio
async def test_mistake_without_trade_id_is_skipped(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    payload = {
        "trade_id": None,
        "mistakes": [],
        "created_at": "x",
    }  # e.g. a DAILY review entry
    await event_bus.publish(
        Event(topic="journal.mistake_recorded", payload=payload, source_module="test")
    )

    await asyncio.sleep(0.3)
    assert driver.calls == []

    await writer.shutdown()


@pytest.mark.asyncio
async def test_update_symbol_correlation_direct_call(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    await writer.update_symbol_correlation(
        "BTCUSDT", "ETHUSDT", 0.85, "2026-07-11T00:00:00Z"
    )

    assert len(driver.calls) == 1
    query, params = driver.calls[0]
    assert query == CORRELATION_QUERY
    assert params["coefficient"] == 0.85

    await writer.shutdown()


@pytest.mark.asyncio
async def test_write_failure_is_isolated_and_counted(event_bus):
    class FailingDriver(FakeDriver):
        def session(self):
            raise ConnectionError("neo4j unreachable")

    driver = FailingDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    await writer.update_symbol_correlation("BTCUSDT", "ETHUSDT", 0.5, "x")

    health = await writer.health_check()
    assert health.details["errors"] == 1


@pytest.mark.asyncio
async def test_health_check_reports_writes_applied(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    await writer.update_symbol_correlation("BTCUSDT", "ETHUSDT", 0.5, "x")
    health = await writer.health_check()
    assert health.details["writes_applied"] == 1
