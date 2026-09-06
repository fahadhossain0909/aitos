import asyncio

import pytest

from aitos.core.contracts import Event
from aitos.knowledge_graph.writer import (
    PROJECT_SEMANTIC_EVENT_QUERY,
    SEMANTIC_TOPICS,
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

    def session(self):
        return FakeSession(self.calls)

    async def close(self):
        return None


async def _wait_for(predicate, timeout=3.0):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.05)
        elapsed += 0.05
    return False


@pytest.mark.asyncio
async def test_semantic_event_projects_decision_lineage(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})

    await event_bus.publish(
        Event(
            topic="decision.trade_candidate",
            payload={
                "symbol": "BTCUSDT",
                "strategy_id": "scanner-v2",
                "model_id": "probability-v1",
                "policy_id": "policy-v3",
                "trade_id": "t42",
                "decision_id": "d42",
                "regime": "trending",
                "score": 82.5,
            },
            source_module="decision-layer",
        )
    )

    assert await _wait_for(lambda: len(driver.calls) == 1)
    query, params = driver.calls[0]
    assert query == PROJECT_SEMANTIC_EVENT_QUERY
    assert params["symbol"] == "BTCUSDT"
    assert params["strategy_id"] == "scanner-v2"
    assert params["model_id"] == "probability-v1"
    assert params["policy_id"] == "policy-v3"
    assert params["trade_id"] == "t42"
    assert params["decision_id"] == "d42"
    assert params["regime"] == "trending"

    await writer.shutdown()


@pytest.mark.asyncio
async def test_semantic_subscriptions_are_live_only(event_bus):
    driver = FakeDriver()
    writer = KnowledgeGraphWriter(event_bus=event_bus, driver=driver)
    await writer.initialize({})
    assert len(writer._subscriptions) == 3 + len(SEMANTIC_TOPICS)
    await writer.shutdown()
