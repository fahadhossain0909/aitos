"""Explicit local projector with injected sinks; no real account/network service."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aitos.console_api import register_console_routes
from aitos.projection_evidence import public_projection, unavailable
from aitos.trading.revision_journal import RevisionJournal, Scope, JournalIntegrityError
from aitos.trading.revision_projector import RevisionProjector, ProjectorBusy


class Sink:
    def __init__(self):
        self.rows = []
        self.insertions = 0

    async def lookup(self, batch):
        return list(self.rows)

    async def insert(self, batch):
        self.insertions += 1
        self.rows.extend(batch)


@pytest.fixture
def setup(tmp_path):
    journal = RevisionJournal(tmp_path / "projector-only.sqlite")
    writer = journal.acquire(
        Scope("paper", "synthetic", "account", "run"), "writer", expected_generation=0
    )
    rows = [
        journal.append(
            writer,
            trade_id="one",
            event_id=f"e{i}",
            expected_revision=i,
            payload={
                "trade_id": "one",
                "state": "position_opened",
                "quantity": 1 - i / 10,
            },
        ).event
        for i in range(2)
    ]
    return journal, writer, rows


@pytest.mark.asyncio
async def test_insert_readback_then_atomic_ack_and_empty_repeat(setup):
    journal, writer, rows = setup
    sink = Sink()
    projector = RevisionProjector(journal, writer, sink)
    assert projector.public_status()["reason"] == "not_observed"
    report = await projector.run_once()
    assert report["status"] == "verified" and report["counts"] == {
        "total": 2,
        "pending": 0,
        "delivered": 2,
    }
    assert report["last_batch"] == {"checked": 2, "inserted": 2, "acknowledged": 2}
    assert sink.rows == rows and not report["production_certified"]
    assert (await projector.run_once())["status"] == "empty" and sink.insertions == 1


@pytest.mark.asyncio
async def test_partial_prior_sink_acceptance_only_inserts_missing(setup):
    journal, writer, rows = setup
    sink = Sink()
    sink.rows = [rows[0]]
    result = await RevisionProjector(journal, writer, sink).run_once()
    assert result["last_batch"]["inserted"] == 1 and sink.rows == rows


@pytest.mark.asyncio
async def test_conflicting_same_revision_or_event_id_prevents_insert_and_ack(setup):
    journal, writer, rows = setup
    for corrupt in [
        replace(rows[0], writer_id="different"),
        replace(rows[0], revision=3),
    ]:
        sink = Sink()
        sink.rows = [corrupt]
        report = await RevisionProjector(journal, writer, sink).run_once()
        assert report["status"] == "conflict" and report["counts"] is None
        assert (
            sink.insertions == 0 and journal.outbox_counts(writer.scope)["pending"] == 2
        )


@pytest.mark.asyncio
async def test_insert_response_lost_no_ack_and_retry_uses_existing_evidence(setup):
    journal, writer, rows = setup

    class LostResponse(Sink):
        async def insert(self, batch):
            await super().insert(batch)
            raise asyncio.TimeoutError("not-for-browser secret error")

    sink = LostResponse()
    projector = RevisionProjector(journal, writer, sink)
    assert (await projector.run_once())["status"] == "timeout"
    assert len(journal.pending(writer.scope)) == 2
    report = await RevisionProjector(
        RevisionJournal(journal.path), writer, sink
    ).run_once()
    assert report["status"] == "verified" and report["last_batch"]["inserted"] == 0
    assert sink.insertions == 1 and sink.rows == rows


@pytest.mark.asyncio
async def test_unconfirmed_insert_and_extra_unrelated_evidence_block_ack(setup):
    journal, writer, rows = setup

    class Missing(Sink):
        async def insert(self, batch):
            pass

    class Unrelated(Sink):
        async def lookup(self, batch):
            return [replace(rows[0], trade_id="other", event_id="other")]

    for sink in [Missing(), Unrelated()]:
        report = await RevisionProjector(journal, writer, sink).run_once()
        assert (
            report["status"] == "conflict" and len(journal.pending(writer.scope)) == 2
        )


@pytest.mark.asyncio
async def test_takeover_during_sink_insert_cannot_ack(setup):
    journal, writer, rows = setup

    class Takeover(Sink):
        async def insert(self, batch):
            await super().insert(batch)
            journal.acquire(writer.scope, "next-writer", expected_generation=1)

    sink = Takeover()
    result = await RevisionProjector(journal, writer, sink).run_once()
    assert result["status"] == "fenced" and sink.rows == rows
    assert journal.outbox_counts(writer.scope)["pending"] == 2


@pytest.mark.asyncio
async def test_slow_lookup_times_out_and_cancellation_clears_evidence(setup):
    journal, writer, _ = setup
    entered = asyncio.Event()

    class Slow(Sink):
        async def lookup(self, batch):
            entered.set()
            await asyncio.Event().wait()

    p = RevisionProjector(journal, writer, Slow(), timeout=0.02)
    assert (await p.run_once())["status"] == "timeout"
    p = RevisionProjector(journal, writer, Slow())
    entered.clear()
    task = asyncio.create_task(p.run_once())
    await entered.wait()
    with pytest.raises(ProjectorBusy):
        await p.run_once()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        p.public_status()["status"] == "cancelled"
        and len(journal.pending(writer.scope)) == 2
    )


def test_batch_ack_all_or_none_and_forced_database_failure(setup):
    journal, writer, rows = setup
    with pytest.raises(JournalIntegrityError):
        journal.acknowledge_batch(writer, [rows[0], replace(rows[1], digest="forged")])
    assert journal.outbox_counts(writer.scope)["delivered"] == 0
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "CREATE TRIGGER fail_second_ack BEFORE UPDATE ON revision_outbox WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT,'test-only'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        journal.acknowledge_batch(writer, rows)
    assert journal.outbox_counts(writer.scope)["delivered"] == 0


@pytest.mark.asyncio
async def test_public_snapshot_observer_requires_auth_and_strips_extras(setup):
    journal, writer, _ = setup
    p = RevisionProjector(journal, writer, Sink())
    await p.run_once()
    p.observation["private_secret"] = "MUST_NOT_APPEAR"
    with pytest.raises(ValueError):
        register_console_routes(web.Application(), projection_observer=p)
    app = web.Application()
    token = "dummy-not-real-" + ("x" * 32)
    register_console_routes(app, projection_observer=p, token=token)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/console/v1/snapshot")).status == 401
        response = await client.get(
            "/console/v1/snapshot", headers={"Authorization": f"Bearer {token}"}
        )
        body = await response.json()
        assert body["projection"]["counts"]["delivered"] == 2
        assert "MUST_NOT_APPEAR" not in json.dumps(body)
        assert response.headers["Cache-Control"] == "no-store"
        p.observation["counts"]["delivered"] = 99
        assert (
            await (
                await client.get(
                    "/console/v1/snapshot", headers={"Authorization": f"Bearer {token}"}
                )
            ).json()
        )["projection"]["available"] is False


def fixture_report():
    return dict(
        schema_version=1,
        scope="local_projection_observation",
        available=True,
        status="verified",
        reason="none",
        observed_at="2026-11-06T12:00:00+00:00",
        counts=dict(total=12, pending=3, delivered=9),
        last_batch=dict(checked=4, inserted=3, acknowledged=4),
        source="local_journal",
        controls="read_only",
        live_authorized=False,
        production_certified=False,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(live_authorized=True),
        lambda r: r.update(production_certified=True),
        lambda r: r["counts"].update(pending=-1),
        lambda r: r["counts"].update(total=13),
        lambda r: r["last_batch"].update(checked=101),
        lambda r: r["last_batch"].update(acknowledged=0),
        lambda r: r["counts"].update(total=True),
        lambda r: r.update(observed_at="not-a-date"),
        lambda r: r.update(status="empty"),
        lambda r: r.update(source="sample"),
    ],
)
def test_observation_validation_fails_closed(mutate):
    report = fixture_report()
    mutate(report)
    safe = public_projection(report, now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
    assert not safe["available"] and safe["counts"] is None


def test_freshness_and_fixture_matches_real_python_producer():
    report = fixture_report()
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    assert public_projection(report, now=now) == report
    assert (
        public_projection(report, now=now + timedelta(seconds=61))["reason"] == "stale"
    )
    assert (
        public_projection(report, now=now - timedelta(seconds=6))["reason"] == "stale"
    )
    root = Path(__file__).resolve().parents[1]
    assert report == json.loads(
        (root / "desktop/src/projection.fixture.json").read_text()
    )
    assert public_projection(unavailable()) == unavailable()


class DurableTestSink:
    """Test-only fsynced JSON receipt store, NOT a production network sink."""

    def __init__(self, path):
        self.path = Path(path)

    async def lookup(self, batch):
        from aitos.trading.revision_journal import Revision

        if not self.path.exists():
            return []
        return [Revision(**row) for row in json.loads(self.path.read_text())]

    async def insert(self, batch):
        import os

        records = await self.lookup(batch)
        with self.path.open("w") as stream:
            json.dump([record.__dict__ for record in [*records, *batch]], stream)
            stream.flush()
            os.fsync(stream.fileno())


def _exit_before_ack(path, writer, sink_path):
    import os

    journal = RevisionJournal(path)

    def die(*args):
        os._exit(23)

    journal.acknowledge_batch = die
    asyncio.run(
        RevisionProjector(journal, writer, DurableTestSink(sink_path)).run_once()
    )
    os._exit(24)


@pytest.mark.asyncio
async def test_actual_projector_process_exit_then_reopen_verifies_without_reinsert(
    setup, tmp_path
):
    import multiprocessing as mp

    journal, writer, events = setup
    sink_path = tmp_path / "synthetic-durable-sink.json"
    process = mp.get_context("spawn").Process(
        target=_exit_before_ack, args=(str(journal.path), writer, str(sink_path))
    )
    try:
        process.start()
        await asyncio.to_thread(process.join, 20)
        assert process.exitcode == 23
        assert len(journal.pending(writer.scope)) == 2
        before = sink_path.read_text()
        projector = RevisionProjector(
            RevisionJournal(journal.path), writer, DurableTestSink(sink_path)
        )
        report = await projector.run_once()
        assert report["last_batch"] == dict(checked=2, inserted=0, acknowledged=2)
        assert sink_path.read_text() == before
        assert len(await DurableTestSink(sink_path).lookup(events)) == 2
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)


@pytest.mark.parametrize(
    "settings",
    [
        {"batch_size": 0},
        {"batch_size": 101},
        {"batch_size": True},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("inf")},
        {"timeout": float("nan")},
        {"timeout": True},
    ],
)
def test_worker_settings_are_strictly_bounded(setup, settings):
    journal, writer, _ = setup
    with pytest.raises(ValueError):
        RevisionProjector(journal, writer, Sink(), **settings)
