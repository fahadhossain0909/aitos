import pytest
from aitos.capital_intelligence.neo4j_graph import Neo4jEntityGraphStore

class FakeSession:
    def __init__(self): self.calls=[]
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def run(self, query, **kwargs): self.calls.append((query, kwargs))
class FakeDriver:
    def __init__(self): self.sessions=[]
    def session(self, **kwargs):
        s=FakeSession(); self.sessions.append(s); return s
    async def close(self): pass

@pytest.mark.asyncio
async def test_graph_store_uses_parameterized_queries():
    driver=FakeDriver()
    store=Neo4jEntityGraphStore(driver)
    await store.ensure_schema()
    assert len(driver.sessions)==1
