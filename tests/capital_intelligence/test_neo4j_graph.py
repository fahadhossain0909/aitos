import pytest
from datetime import datetime, timezone

from aitos.capital_intelligence.contracts import CapitalEvent
from aitos.capital_intelligence.neo4j_graph import Neo4jEntityGraphStore


class FakeSession:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def run(self, query, **kwargs):
        self.calls.append((query, kwargs))


class FakeDriver:
    def __init__(self):
        self.sessions = []

    def session(self, **kwargs):
        s = FakeSession()
        self.sessions.append(s)
        return s

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_graph_store_uses_parameterized_queries():
    driver = FakeDriver()
    store = Neo4jEntityGraphStore(driver)
    await store.ensure_schema()
    assert len(driver.sessions) == 1


@pytest.mark.asyncio
async def test_graph_store_persists_event_observations_without_identity_claims():
    driver = FakeDriver()
    store = Neo4jEntityGraphStore(driver)
    now = datetime.now(timezone.utc)
    event = CapitalEvent(
        "event-1",
        "evm:1",
        "onchain_transfer",
        "crypto_spot",
        "1",
        "TOKEN",
        "erc20_transfer",
        now,
        now,
        10,
        "token",
        25,
        actor_id="0xsender",
        counterparty_id="0xreceiver",
        transaction_id="0xtx",
        metadata={"actor_identity_kind": "evm_address"},
    )
    assert await store.upsert_events([event]) == 1
    query, parameters = driver.sessions[0].calls[0]
    assert "CFEIEvent" in query
    assert "OBSERVED_EVENT" in query
    assert "beneficial_owner" not in query
    assert parameters["rows"][0]["actor_entity_id"] == "1:0xsender"
    assert parameters["rows"][0]["counterparty_entity_id"] == "1:0xreceiver"
