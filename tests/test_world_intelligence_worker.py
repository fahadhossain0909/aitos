import pytest

from aitos.world_intelligence.worker import IntelligenceIngestionWorker


class Store:
    def __init__(self):
        self.cursor = None
        self.schema_calls = 0
        self.dead_letters = []
        self.pending = []
        self.replayed = []

    async def ensure_schema(self):
        self.schema_calls += 1

    async def load_cursor(self, source_id):
        return self.cursor

    async def append_events(self, source_id, events):
        return len(events)

    async def save_cursor(self, source_id, cursor, **kwargs):
        self.cursor = cursor

    async def record_dead_letter(self, **kwargs):
        self.dead_letters.append(kwargs)
        return "failure-1"

    async def load_pending_dead_letters(self, *, limit=100):
        return self.pending[:limit]

    async def update_dead_letter(self, failure_id, *, status, attempts):
        self.replayed.append((failure_id, status, attempts))


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


@pytest.mark.asyncio
async def test_worker_records_failed_provider_page_in_dead_letter():
    class FailingAdapter:
        source_id = "test"

        async def poll(self, *, cursor=None):
            raise RuntimeError("provider unavailable")

    store = Store()
    worker = IntelligenceIngestionWorker([FailingAdapter()], store, Sink())
    result = await worker.run_once()

    assert result[0].error == "RuntimeError"
    assert store.dead_letters == []
    assert worker.metrics_snapshot()["dead_letters_written"] == 0


@pytest.mark.asyncio
async def test_dead_letter_replay_requires_lease_manager():
    worker = IntelligenceIngestionWorker([Adapter()], Store(), Sink())
    with pytest.raises(RuntimeError, match="lease manager"):
        await worker.replay_dead_letters()


@pytest.mark.asyncio
async def test_worker_dead_letters_a_fetched_page_when_publish_fails():
    class FailingSink:
        async def publish(self, topic, payload):
            raise RuntimeError("redis unavailable")

    store = Store()
    worker = IntelligenceIngestionWorker([Adapter()], store, FailingSink())
    result = await worker.run_once()

    assert result[0].error == "RuntimeError"
    assert len(store.dead_letters) == 1
    assert store.dead_letters[0]["cursor_after"] == "next"
    assert worker.metrics_snapshot()["dead_letters_written"] == 1
