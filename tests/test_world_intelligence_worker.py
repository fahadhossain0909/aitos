from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.worker import IntelligenceIngestionWorker

def make_event():
    # Keep the fixture inside the production 72-hour ingestion window.
    now = datetime.now(timezone.utc)
    source = SourceEvidence(
        source_id="test",
        source_kind="news",
        uri="https://example.test/story",
        published_at=now - timedelta(seconds=1),
        first_seen_at=now,
        reliability=0.8,
    )
    return IntelligenceEvent(
        event_id="event-1",
        event_type="news",
        title="Test event",
        entities=("BTC",),
        published_at=source.published_at,
        first_seen_at=now,
        source=source,
        sentiment=0.0,
        severity=0.2,
        novelty=0.1,
        confidence=0.5,
    )


class Store:
    def __init__(self):
        self.cursor = None
        self.schema_calls = 0
        self.dead_letters = []
        self.pending = []
        self.replayed = []
        self.source_runs = []

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

    async def record_source_run(self, **kwargs):
        self.source_runs.append(kwargs)

    async def load_source_run_summary(self, **kwargs):
        return [{"source_id": "test", "runs": len(self.source_runs)}]


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
    assert store.source_runs[0]["status"] == "success"


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
    class FailingAdapter:
        source_id = "test"

        async def poll(self, *, cursor=None):
            return [make_event()], "next"

    class FailingSink:
        async def publish(self, topic, payload):
            raise RuntimeError("redis unavailable")

    store = Store()
    worker = IntelligenceIngestionWorker([FailingAdapter()], store, FailingSink())
    result = await worker.run_once()

    assert result[0].error == "RuntimeError"
    assert len(store.dead_letters) == 1
    assert store.dead_letters[0]["cursor_after"] == "next"
    assert worker.metrics_snapshot()["dead_letters_written"] == 1
    assert store.source_runs[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_dead_letter_replay_saves_fenced_cursor_and_marks_page_replayed():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    class LeaseManager:
        @asynccontextmanager
        async def hold(self, source_id):
            yield SimpleNamespace(
                fencing_token=12,
                assert_valid=lambda: _valid_lease(),
            )

    async def _valid_lease():
        return None

    store = Store()
    store.cursor = "cursor-1"
    store.pending = [
        {
            "failure_id": "failure-1",
            "source_id": "test",
            "cursor_before": "cursor-1",
            "cursor_after": "cursor-2",
            "events": [],
            "attempts": 1,
        }
    ]
    worker = IntelligenceIngestionWorker(
        [Adapter()], store, Sink(), lease_manager=LeaseManager()
    )

    assert await worker.replay_dead_letters() == 1
    assert store.cursor == "cursor-2"
    assert store.replayed == [("failure-1", "replayed", 2)]
    assert worker.metrics_snapshot()["dead_letters_replayed"] == 1


@pytest.mark.asyncio
async def test_dead_letter_replay_never_rewinds_an_advanced_cursor():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    class LeaseManager:
        @asynccontextmanager
        async def hold(self, source_id):
            yield SimpleNamespace(
                fencing_token=13,
                assert_valid=lambda: _valid_lease(),
            )

    async def _valid_lease():
        return None

    store = Store()
    store.cursor = "cursor-3"
    store.pending = [
        {
            "failure_id": "stale-failure",
            "source_id": "test",
            "cursor_before": "cursor-1",
            "cursor_after": "cursor-2",
            "events": [],
            "attempts": 1,
        }
    ]
    worker = IntelligenceIngestionWorker(
        [Adapter()], store, Sink(), lease_manager=LeaseManager()
    )

    assert await worker.replay_dead_letters() == 0
    assert store.cursor == "cursor-3"
    assert store.replayed == [("stale-failure", "quarantined", 2)]
    assert worker.metrics_snapshot()["dead_letters_quarantined"] == 1


@pytest.mark.asyncio
async def test_worker_survives_redis_lease_service_outage():
    class BrokenLeaseManager:
        def hold(self, source_id):
            raise RuntimeError("redis connection unavailable")

    worker = IntelligenceIngestionWorker(
        [Adapter()], Store(), Sink(), lease_manager=BrokenLeaseManager()
    )
    result = await worker.run_once()

    assert result[0].error == "RuntimeError"
    assert worker.metrics_snapshot()["lease_lost"] == 1


@pytest.mark.asyncio
async def test_worker_exposes_durable_source_run_summary():
    store = Store()
    worker = IntelligenceIngestionWorker([Adapter()], store, Sink())
    await worker.run_once()

    summary = await worker.source_run_summary()
    assert summary == [{"source_id": "test", "runs": 1}]


@pytest.mark.asyncio
async def test_dead_letter_retry_limit_moves_poison_page_to_exhausted():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    class LeaseManager:
        @asynccontextmanager
        async def hold(self, source_id):
            yield SimpleNamespace(fencing_token=14, assert_valid=lambda: _valid())

    async def _valid():
        return None

    store = Store()
    store.pending = [
        {
            "failure_id": "poison-page",
            "source_id": "test",
            "cursor_before": "cursor-1",
            "cursor_after": "cursor-2",
            "events": [],
            "attempts": 5,
        }
    ]
    worker = IntelligenceIngestionWorker(
        [Adapter()], store, Sink(), lease_manager=LeaseManager()
    )

    assert await worker.replay_dead_letters(max_attempts=5) == 0
    assert store.replayed == [("poison-page", "exhausted", 5)]
    assert worker.metrics_snapshot()["dead_letters_exhausted"] == 1


@pytest.mark.asyncio
async def test_dead_letter_cursor_conflict_is_quarantined_not_retried():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    class LeaseManager:
        @asynccontextmanager
        async def hold(self, source_id):
            yield SimpleNamespace(fencing_token=15, assert_valid=lambda: _valid())

    async def _valid():
        return None

    store = Store()
    store.cursor = "cursor-advanced"
    store.pending = [
        {
            "failure_id": "stale-page",
            "source_id": "test",
            "cursor_before": "cursor-1",
            "cursor_after": "cursor-2",
            "events": [],
            "attempts": 1,
        }
    ]
    worker = IntelligenceIngestionWorker(
        [Adapter()], store, Sink(), lease_manager=LeaseManager()
    )

    assert await worker.replay_dead_letters(max_attempts=5) == 0
    assert store.cursor == "cursor-advanced"
    assert store.replayed == [("stale-page", "quarantined", 2)]
    assert worker.metrics_snapshot()["dead_letters_quarantined"] == 1


@pytest.mark.asyncio
async def test_dead_letter_retry_limit_is_validated():
    worker = IntelligenceIngestionWorker([Adapter()], Store(), Sink())
    with pytest.raises(ValueError, match="max_attempts"):
        await worker.replay_dead_letters(max_attempts=0)


@pytest.mark.asyncio
async def test_dead_letter_lease_loss_does_not_consume_retry_budget():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from aitos.world_intelligence.lease import LeaseLost

    class LeaseManager:
        @asynccontextmanager
        async def hold(self, source_id):
            async def _lost():
                raise LeaseLost("lease heartbeat failed")

            yield SimpleNamespace(fencing_token=16, assert_valid=_lost)

    store = Store()
    store.pending = [
        {
            "failure_id": "lease-interrupted",
            "source_id": "test",
            "cursor_before": "cursor-1",
            "cursor_after": "cursor-2",
            "events": [],
            "attempts": 2,
        }
    ]
    worker = IntelligenceIngestionWorker(
        [Adapter()], store, Sink(), lease_manager=LeaseManager()
    )

    assert await worker.replay_dead_letters(max_attempts=3) == 0
    assert store.replayed == []
    assert worker.metrics_snapshot()["lease_lost"] == 1
