import pytest

from aitos.world_intelligence.worker import IntelligenceIngestionWorker


class Store:
    def __init__(self):
        self.cursor = None
        self.schema_calls = 0

    async def ensure_schema(self):
        self.schema_calls += 1

    async def load_cursor(self, source_id):
        return self.cursor

    async def append_events(self, source_id, events):
        return len(events)

    async def save_cursor(self, source_id, cursor):
        self.cursor = cursor


class Adapter:
    source_id = "test"

    async def poll(self, *, cursor=None):
        return [], "next"


class Sink:
    async def publish(self, topic, payload):
        pass


@pytest.mark.asyncio
async def test_worker_runs_sources_and_initializes_schema():
    store = Store()
    worker = IntelligenceIngestionWorker(
        [Adapter()], store, Sink(), poll_interval_seconds=1
    )
    await store.ensure_schema()
    result = await worker.run_once()
    assert store.schema_calls == 1
    assert result[0].source_id == "test"
    assert result[0].cursor == "next"


@pytest.mark.asyncio
async def test_worker_rejects_duplicate_source_adapters():
    with pytest.raises(ValueError, match="one adapter"):
        IntelligenceIngestionWorker([Adapter(), Adapter()], Store(), Sink())


@pytest.mark.asyncio
async def test_worker_run_once_is_non_reentrant():
    store = Store()
    worker = IntelligenceIngestionWorker([Adapter()], store, Sink())
    worker._running = True
    with pytest.raises(RuntimeError, match="not reentrant"):
        await worker.run_once()
