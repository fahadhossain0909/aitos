"""Opt-in runner integration; synthetic records, no venue/real Redis services."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aitos.trading.paper_shadow_runtime import (
    PaperShadowRuntime,
    MirroredPaperStateStore,
    configured_shadow,
)
from aitos.trading.paper_revision import PaperRevisionStore, encode_trade
from aitos.trading.revision_journal import RevisionJournal, Scope, RevisionConflict
from tests.test_trade_revision_binding import sample_trade


class Sink:
    def __init__(self):
        self.rows = []

    async def lookup(self, batch):
        return [
            r
            for r in self.rows
            if any(
                r.event_id == e.event_id
                or (r.trade_id, r.revision) == (e.trade_id, e.revision)
                for e in batch
            )
        ]

    async def insert(self, batch):
        self.rows.extend(batch)


@pytest.fixture
def setup(tmp_path):
    journal = RevisionJournal(tmp_path / "shadow.sqlite")
    writer = journal.acquire(
        Scope("paper", "synthetic", "account", "run"), "writer", expected_generation=0
    )
    return PaperShadowRuntime(
        PaperRevisionStore(journal, writer), Sink(), interval=0.05
    )


async def test_real_lifecycle_event_to_shadow_and_projector(setup):
    from aitos.trading.persistent_state import TradeStatePersistence
    from aitos.core.contracts import Event

    legacy = SimpleNamespace(save_trade=AsyncMock(), initialize=AsyncMock())
    lifecycle = SimpleNamespace()
    persistence = TradeStatePersistence(
        None, lifecycle, MirroredPaperStateStore(legacy, setup)
    )
    trade = sample_trade()
    await persistence._save_event(
        Event(
            topic="trade.position_opened",
            source_module="synthetic",
            payload=encode_trade(trade),
        )
    )
    legacy.save_trade.assert_awaited_once()
    await setup.start()
    try:
        for _ in range(100):
            delivered = setup.store.journal.outbox_counts(
                setup.store.writer.scope
            )["delivered"]
            if delivered == 1 and setup.public_status()["available"]:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("worker never published verified projection status")
    finally:
        await setup.stop()
    assert setup._task.done()


async def test_exact_redelivery_and_historical_redelivery_preserve_head(setup):
    first = sample_trade()
    second = replace(first, updated_at="2026-11-01T00:00:02Z", sl_price=99)
    assert await setup.record(first)
    assert await setup.record(second)
    assert await setup.record(first)
    assert (
        setup.store.journal.head(setup.store.writer.scope, first.trade_id).revision == 2
    )
    assert setup.store.journal.find_event(setup.store.writer.scope, "missing") is None


async def test_stale_changed_snapshot_latches_unavailable_without_overwrite(setup):
    first = sample_trade()
    assert await setup.record(first)
    assert not await setup.record(
        replace(first, updated_at="2026-10-31T00:00:00Z", sl_price=99)
    )
    assert setup.public_status()["counts"] is None
    assert not await setup.record(first)
    assert setup.store.recover().open_trades[0].sl_price == first.sl_price


async def test_takeover_latches_and_does_not_ack(setup):
    await setup.record(sample_trade())
    setup.store.journal.acquire(
        setup.store.writer.scope, "replacement", expected_generation=1
    )
    await setup.start()
    await asyncio.sleep(0.08)
    await setup.stop()
    assert not setup.public_status()["available"]
    assert len(setup.store.journal.pending(setup.store.writer.scope)) == 1


async def test_legacy_failure_never_creates_shadow_evidence(setup):
    legacy = SimpleNamespace(save_trade=AsyncMock(side_effect=OSError("synthetic")))
    with pytest.raises(OSError):
        await MirroredPaperStateStore(legacy, setup).save_trade(sample_trade())
    assert not setup.store.journal.heads(setup.store.writer.scope)


async def test_shadow_failure_does_not_disable_legacy_exit_archival(setup):
    legacy = SimpleNamespace(save_closed_trade=AsyncMock(), delete_trade=AsyncMock())
    setup._failed = True
    wrapped = MirroredPaperStateStore(legacy, setup)
    await wrapped.save_closed_trade(sample_trade())
    await wrapped.delete_trade("synthetic-1")
    legacy.save_closed_trade.assert_awaited_once()
    legacy.delete_trade.assert_awaited_once()
    assert not setup.public_status()["available"]


async def test_start_stop_is_explicit_and_idempotent_stop(setup):
    await setup.stop()
    await setup.stop()
    with pytest.raises(RuntimeError):
        await setup.start()


async def test_second_start_rejected(setup):
    await setup.start()
    try:
        with pytest.raises(RuntimeError):
            await setup.start()
    finally:
        await setup.stop()


@pytest.mark.parametrize("interval", [0, True, float("nan"), 31])
def test_interval_validation(setup, interval):
    with pytest.raises(ValueError):
        PaperShadowRuntime(setup.store, Sink(), interval=interval)


def test_disabled_configuration_has_no_client_or_file_effects():
    assert configured_shadow({}, None) is None
    with pytest.raises(ValueError):
        configured_shadow({"AITOS_PAPER_REVISION_SHADOW": "yes"}, None)


def test_explicit_config_generation_prevents_restart_stealing(tmp_path):
    env = dict(
        AITOS_PAPER_REVISION_SHADOW="1",
        AITOS_CONSOLE_TOKEN="x" * 32,
        AITOS_PAPER_REVISION_PATH=str(tmp_path / "scoped.sqlite"),
        AITOS_PAPER_REVISION_PROVIDER="synthetic",
        AITOS_PAPER_REVISION_ACCOUNT="account",
        AITOS_PAPER_REVISION_RUN="run",
        AITOS_PAPER_REVISION_WRITER="one",
        AITOS_PAPER_REVISION_EXPECTED_GENERATION="0",
    )
    runtime = configured_shadow(env, object())
    assert runtime.store.writer.generation == 1
    with pytest.raises(RevisionConflict):
        configured_shadow(env, object())
    env["AITOS_PAPER_REVISION_EXPECTED_GENERATION"] = "1"
    env["AITOS_PAPER_REVISION_WRITER"] = "two"
    assert configured_shadow(env, object()).store.writer.generation == 2


async def test_health_server_forwards_observer_and_requires_auth(setup):
    from aitos.health_server import HealthServer
    from aiohttp.test_utils import TestClient, TestServer

    await setup.record(sample_trade())
    await setup.projector.run_once()
    client = TestClient(
        TestServer(
            HealthServer(
                [], console_token="x" * 32, console_projection_observer=setup
            )._build_app()
        )
    )
    await client.start_server()
    try:
        assert (await client.get("/console/v1/snapshot")).status == 401
        response = await client.get(
            "/console/v1/snapshot", headers={"Authorization": "Bearer " + "x" * 32}
        )
        body = await response.json()
        assert body["projection"]["counts"]["delivered"] == 1
    finally:
        await client.close()


async def test_cancelled_record_and_shutdown_drain_sqlite_thread(setup):
    import threading

    entered, release = threading.Event(), threading.Event()
    original = setup.store.save_trade

    def slow(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test did not release writer")
        return original(*args, **kwargs)

    setup.store.save_trade = slow
    task = asyncio.create_task(setup.record(sample_trade()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        stopping = asyncio.create_task(setup.stop())
        await asyncio.sleep(0.02)
        assert not stopping.done()
        task.cancel()  # Repeated cancellation must not cancel the native IO task.
        await asyncio.sleep(0.02)
        assert not task.done() and not stopping.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stopping
    assert len(setup.store.journal.pending(setup.store.writer.scope)) == 1
    assert not setup.public_status()["available"]
    assert not await setup.record(sample_trade())


async def test_actual_close_callback_retains_terminal_journal_record(setup):
    from aitos.models.trade import TradeLifecycleState
    from aitos.trading.persistent_state import TradeStatePersistence
    from aitos.core.contracts import Event

    trade = sample_trade()
    assert await setup.record(trade)
    closed = replace(
        trade,
        state=TradeLifecycleState.POSITION_CLOSED,
        exit_price=110,
        exit_time="2026-11-01T00:01:00Z",
        updated_at="2026-11-01T00:01:00Z",
        gross_realized_pnl_usd=10,
        fees_paid_usd=1,
        funding_pnl_usd=-0.5,
        pnl=8.5,
    )
    legacy = SimpleNamespace(save_closed_trade=AsyncMock(), delete_trade=AsyncMock())
    persistence = TradeStatePersistence(
        None, SimpleNamespace(), MirroredPaperStateStore(legacy, setup)
    )
    await persistence._delete_event(
        Event(topic="trade.position_closed", payload=encode_trade(closed))
    )
    legacy.delete_trade.assert_awaited_once_with(closed.trade_id)
    recovery = setup.store.recover()
    assert not recovery.open_trades and recovery.closed_trades == (closed,)
    assert recovery.records[0].revision == 2


async def test_record_captures_snapshot_before_mutable_trade_changes(setup):
    import threading

    entered, release = threading.Event(), threading.Event()
    original = setup.store.save_trade

    def delayed(trade, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("writer not released")
        return original(trade, **kwargs)

    setup.store.save_trade = delayed
    trade = sample_trade()
    recording = asyncio.create_task(setup.record(trade))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        trade.quantity = 2
        trade.position_size_usd = 200
        trade.risk_amount_usd = 4
    finally:
        release.set()
    assert await recording
    retained = setup.store.recover().open_trades[0]
    assert retained.quantity == 1 and retained.position_size_usd == 100


async def test_queued_record_captures_before_lock_wait(setup):
    trade = sample_trade()
    trade.agent_consensus = {"audit": {"tag": "before"}}
    async with setup._lock:
        queued = asyncio.create_task(setup.record(trade))
        await asyncio.sleep(0.01)
        trade.quantity = 2
        trade.position_size_usd = 200
        trade.risk_amount_usd = 4
        trade.agent_consensus["audit"]["tag"] = "after"
    assert await queued
    retained = setup.store.recover().open_trades[0]
    assert (
        retained.quantity == 1 and retained.agent_consensus["audit"]["tag"] == "before"
    )


async def test_cancelled_native_future_fails_closed_without_terminal_spin(setup):
    def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    setup.store.save_trade = cancelled
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(setup.record(sample_trade()), 2)
    assert not setup.public_status()["available"]
    await setup.stop()
