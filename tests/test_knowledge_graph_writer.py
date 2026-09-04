import asyncio

import pytest

from aitos.core.contracts import Event
from aitos.knowledge_graph.writer import (
    CLOSE_TRADE_QUERY,
    CORRELATION_QUERY,
    CREATE_TRADE_QUERY,
    LINK_MISTAKE_QUERY,
    PROJECT_SEMANTIC_EVENT_QUERY,
    KnowledgeGraphWriter,
)


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
        "trade_id": "t1", "symbol": "BTCUSDT", "strategy_id": "scanner-v1",
        "side": "LONG", "entry_price": 100.0, "regime": "trending",
        "state": "position_opened", "entry_time": "2026-07-11T00:00:00Z",
    }
    await event_bus.publish(Event(topic="trade.position_opened", payload=payload, source_module="test"))

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

    payload = {"trade_id": "t1", "pnl": 150.0, "pnl_percent": 15.0,
               "exit_price": 104.0, "exit_reason": "tp_triggered",
               "exit_time": "x", "state": "position_closed"}
    await event_bus.publish(Event(topic="trade.position_closed", payload=payload, source_module="test"))

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
    await event_bus.publish(Event(topic="journal.mistake_recorded", payload=payload, source_module="test"))

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
    await event_bus.publish(Event(topic="journal.mistake_recorded", payload={"trade_id": None, "mistakes": [], "created_at": "x"}, source_module="test"))
    await asyncio.sleep(0.3)
    assert driver.calls == []
    await writer.shutdown()


@pytest.mark.asyncio
async def test_update_symbol_correlation_direct_call(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})
    await writer.update_symbol_correlation("BTCUSDT", "ETHUSDT", 0.85, "2026-07-11T00:00:00Z")
    assert len(driver.calls) == 1
    query, params = driver.calls[0]
    assert query == CORRELATION_QUERY
    assert params["coefficient"] == 0.85
    await writer.shutdown()


@pytest.mark.asyncio
async def test_semantic_event_projects_decision_and_learning_lineage(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    payload = {
        "symbol": "BTCUSDT",
        "strategy_id": "momentum-v2",
        "model_id": "context-v3",
        "model_run_id": "run-42",
        "policy_id": "policy-v7",
        "decision_id": "decision-42",
        "risk_decision_id": "risk-42",
        "execution_id": "exec-42",
        "journey_id": "journey-42",
        "forecast_id": "forecast-42",
        "outcome_id": "outcome-42",
        "calibration_id": "cal-42",
        "regime": "trending",
        "probability": 0.82,
        "target": "up_15m",
        "horizon": "15m",
        "outcome": "correct",
        "pnl": 125.0,
        "risk_action": "approve",
        "risk_score": 0.91,
        "evidence": [
            {"name": "cvd", "value": 1.7, "weight": 0.4},
            "liquidity_imbalance",
        ],
    }
    await event_bus.publish(Event(topic="decision.generated", payload=payload, source_module="contextual-ai"))

    assert await _wait_for(lambda: len(driver.calls) == 1)
    query, params = driver.calls[0]
    assert query == PROJECT_SEMANTIC_EVENT_QUERY
    assert params["decision_id"] == "decision-42"
    assert params["model_id"] == "context-v3"
    assert params["risk_id"] == "risk-42"
    assert params["execution_id"] == "exec-42"
    assert params["journey_id"] == "journey-42"
    assert params["forecast_probability"] == 0.82
    assert params["calibration_id"] == "cal-42"
    assert len(params["evidence"]) == 2
    assert params["evidence"][0]["name"] == "cvd"
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
    await writer.shutdown()


@pytest.mark.asyncio
async def test_health_check_reports_writes_applied(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})
    await writer.update_symbol_correlation("BTCUSDT", "ETHUSDT", 0.5, "x")
    health = await writer.health_check()
    assert health.details["writes_applied"] == 1
    await writer.shutdown()
