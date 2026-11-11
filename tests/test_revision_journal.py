"""Approved UNWIRED prototype; all state is in pytest temporary files."""

import multiprocessing as mp
import sqlite3
from dataclasses import replace

import pytest

from aitos.trading.revision_journal import (
    MAX_INTEGER,
    AppendResult,
    JournalIntegrityError,
    RevisionConflict,
    RevisionJournal,
    Scope,
    canonical,
)


def scope(account="synthetic-a"):
    return Scope("paper", "synthetic", account, "run-1")


def payload(quantity=1, state="position_opened"):
    return dict(trade_id="trade-1", state=state, quantity=quantity)


def write(journal, writer, event="e1", expected=0, data=None):
    return journal.append(
        writer,
        trade_id="trade-1",
        event_id=event,
        expected_revision=expected,
        payload=payload() if data is None else data,
    )


@pytest.fixture
def setup(tmp_path):
    journal = RevisionJournal(tmp_path / "prototype-only.sqlite")
    writer = journal.acquire(scope(), "writer-a", expected_generation=0)
    return journal, writer


def test_atomic_append_reopen_and_read_copy(setup):
    journal, writer = setup
    event = write(journal, writer).event
    assert event.revision == 1 and len(journal.pending(scope())) == 1
    journal = RevisionJournal(journal.path)
    assert journal.head(scope(), "trade-1") == event
    data = event.payload
    data["quantity"] = 999
    assert event.payload["quantity"] == 1
    with sqlite3.connect(journal.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with journal._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_duplicate_ack_does_not_roll_back_newer_head(setup):
    journal, writer = setup
    first = write(journal, writer)
    write(journal, writer, "e2", 1, payload(0.5))
    duplicate = write(journal, writer)
    assert isinstance(duplicate, AppendResult)
    assert (
        duplicate.duplicate
        and duplicate.event == first.event
        and duplicate.head_revision == 2
    )
    assert journal.head(scope(), "trade-1").payload["quantity"] == 0.5
    assert len(journal.pending(scope())) == 2


def test_conflicting_id_and_stale_cas_leave_no_partial_writes(setup):
    journal, writer = setup
    write(journal, writer)
    for event, expected, data in [("e1", 1, payload(0.5)), ("e2", 0, payload(0.5))]:
        with pytest.raises(RevisionConflict):
            write(journal, writer, event, expected, data)
    assert journal.head(scope(), "trade-1").revision == 1
    assert len(journal.pending(scope())) == 1


def test_fenced_generation_including_duplicate_and_outbox(setup):
    journal, old = setup
    event = write(journal, old).event
    new = journal.acquire(scope(), "writer-b", expected_generation=1)
    for action in [
        lambda: write(journal, old),
        lambda: journal.acknowledge(old, event),
        lambda: journal.acquire(scope(), "other", expected_generation=1),
    ]:
        with pytest.raises(RevisionConflict):
            action()
    assert write(journal, new).duplicate
    journal.acknowledge(new, event)
    journal.acknowledge(new, event)
    assert journal.pending(scope()) == []


def test_scope_isolation_and_no_cross_scope_receipt(setup):
    journal, a = setup
    b = journal.acquire(scope("synthetic-b"), "writer-b", expected_generation=0)
    first = write(journal, a).event
    second = write(journal, b, data=payload(0.5)).event
    assert first.scope_key != second.scope_key
    assert journal.head(scope("synthetic-b"), "trade-1").payload["quantity"] == 0.5
    with pytest.raises(RevisionConflict):
        journal.acknowledge(b, first)


def test_terminal_snapshot_preserved_not_reopened_or_amended(setup):
    journal, writer = setup
    write(journal, writer)
    terminal = write(journal, writer, "e2", 1, payload(0, "position_closed"))
    assert write(journal, writer, "e2", 1, payload(0, "position_closed")).duplicate
    for data in [payload(), payload(1, "position_closed")]:
        with pytest.raises(RevisionConflict, match="terminal"):
            write(journal, writer, "e3", 2, data)
    assert journal.head(scope(), "trade-1") == terminal.event


@pytest.mark.parametrize("value", [True, -1, MAX_INTEGER + 1, 1.0, None])
def test_invalid_revision_is_rejected_before_state_change(setup, value):
    journal, writer = setup
    with pytest.raises(ValueError):
        write(journal, writer, expected=value)
    assert journal.head(scope(), "trade-1") is None


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), 2**70, {1: "wrong-key"}, (1, 2), object(), "\ud800"],
)
def test_invalid_nested_json_is_rejected(setup, bad):
    journal, writer = setup
    data = payload()
    data["extra"] = bad
    with pytest.raises(ValueError):
        write(journal, writer, data=data)
    assert journal.pending(scope()) == []


def test_payload_limits_and_canonicalization(setup):
    journal, writer = setup
    for value in ["x" * 65536, [0] * 10001]:
        data = payload()
        data["extra"] = value
        with pytest.raises(ValueError):
            write(journal, writer, data=data)
    nested = {}
    nested["self"] = nested
    with pytest.raises(ValueError):
        canonical(nested)
    assert canonical({"b": 1, "a": "বাংলা"}) == canonical({"a": "বাংলা", "b": 1})


@pytest.mark.parametrize("mode", ["live", "unknown", None])
def test_live_or_unknown_scope_rejected(mode):
    with pytest.raises(ValueError):
        Scope(mode, "venue", "account", "run")


def test_unknown_existing_database_not_repurposed(tmp_path):
    path = tmp_path / "other.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE user_data(value TEXT)")
    with pytest.raises(JournalIntegrityError):
        RevisionJournal(path)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("user_data",)]


def test_atomic_rollback_when_outbox_insert_fails(setup):
    journal, writer = setup
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "CREATE TRIGGER reject_outbox BEFORE INSERT ON revision_outbox BEGIN SELECT RAISE(ABORT,'test-only-injected-failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        write(journal, writer)
    assert journal.head(scope(), "trade-1") is None
    assert journal.pending(scope()) == []
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM revision_events").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE revision_events SET digest='corrupt'",
        "DELETE FROM revision_heads",
        "DELETE FROM revision_outbox",
        "UPDATE revision_heads SET revision=99",
    ],
)
def test_recovery_blocks_inconsistent_evidence(setup, mutation):
    journal, writer = setup
    write(journal, writer)
    with sqlite3.connect(journal.path) as db:
        db.execute(mutation)
    with pytest.raises(JournalIntegrityError):
        journal.head(scope(), "trade-1")


def test_outbox_crash_window_redelivers_same_immutable_event(setup):
    journal, writer = setup
    original = write(journal, writer).event
    sink = []
    sink.extend(journal.pending(scope()))  # sink insert succeeds, no local ACK
    reopened = RevisionJournal(journal.path)
    sink.extend(reopened.pending(scope()))
    assert sink == [original, original]  # at least once, NOT exactly once
    reopened.acknowledge(writer, original)
    assert reopened.pending(scope()) == []
    with pytest.raises(JournalIntegrityError):
        reopened.acknowledge(writer, replace(original, digest="forged"))


def _race(path, writer, event, ready, start, results):
    journal = RevisionJournal(path)
    ready.put(True)
    if not start.wait(15):
        raise RuntimeError("test barrier timeout")
    try:
        result = write(journal, writer, event)
        results.put(("committed", result.event.event_id))
    except RevisionConflict:
        results.put(("conflict", event))


def test_separate_processes_cas_has_one_winner(setup):
    journal, writer = setup
    ctx = mp.get_context("spawn")
    ready, results = ctx.Queue(), ctx.Queue()
    start = ctx.Event()
    processes = [
        ctx.Process(
            target=_race,
            args=(str(journal.path), writer, f"e{i}", ready, start, results),
        )
        for i in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=20)
        start.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert sorted(kind for kind, _ in outcomes) == ["committed", "conflict"]
        assert journal.head(scope(), "trade-1").revision == 1
        assert len(journal.pending(scope())) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join(5)
        ready.close()
        results.close()


def test_old_history_corruption_and_missing_outbox_fail_closed(setup):
    journal, writer = setup
    write(journal, writer)
    write(journal, writer, "e2", 1, payload(0.5))
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE revision_events SET digest='invalid' WHERE revision=1")
    for read in [
        lambda: journal.head(scope(), "trade-1"),
        lambda: journal.pending(scope()),
    ]:
        with pytest.raises(JournalIntegrityError):
            read()


def test_missing_outbox_cannot_silently_look_delivered(setup):
    journal, writer = setup
    write(journal, writer)
    with sqlite3.connect(journal.path) as db:
        db.execute("DELETE FROM revision_outbox")
    with pytest.raises(JournalIntegrityError):
        journal.pending(scope())


def _crash(path, writer, before_commit):
    import os
    from contextlib import contextmanager

    journal = RevisionJournal(path)
    if before_commit:
        original = journal._connect

        @contextmanager
        def crashing_connection():
            with original() as db:
                db.create_function("test_process_exit", 0, lambda: os._exit(23))
                yield db

        journal._connect = crashing_connection
    write(journal, writer)
    os._exit(24)  # Commit succeeded, application did not observe an acknowledgement.


@pytest.mark.parametrize("before_commit", [True, False])
def test_real_process_death_before_or_after_commit(setup, before_commit):
    journal, writer = setup
    if before_commit:
        with sqlite3.connect(journal.path) as db:
            db.execute(
                "CREATE TRIGGER crash_test BEFORE INSERT ON revision_outbox BEGIN SELECT test_process_exit(); END"
            )
    process = mp.get_context("spawn").Process(
        target=_crash, args=(str(journal.path), writer, before_commit)
    )
    try:
        process.start()
        process.join(20)
        assert process.exitcode == (23 if before_commit else 24)
        reopened = RevisionJournal(journal.path)
        if before_commit:
            assert reopened.head(scope(), "trade-1") is None
            assert reopened.pending(scope()) == []
        else:
            assert reopened.head(scope(), "trade-1").revision == 1
            assert write(reopened, writer).duplicate
            assert len(reopened.pending(scope())) == 1
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)


def test_generation_exhaustion_and_bad_state_leave_state_intact(setup):
    journal, writer = setup
    with pytest.raises(RevisionConflict, match="exhausted"):
        journal.acquire(scope(), "other", expected_generation=MAX_INTEGER)
    for state in [[], None, "unknown"]:
        with pytest.raises(ValueError):
            write(journal, writer, data=payload(state=state))
    assert journal.head(scope(), "trade-1") is None


def test_projection_duplicate_identity_and_batch_bounds(setup):
    from aitos.trading.revision_journal import verify_projection_batch

    journal, writer = setup
    event = write(journal, writer).event
    assert verify_projection_batch([event, event]) == [event]
    with pytest.raises(JournalIntegrityError):
        verify_projection_batch([event, replace(event, revision=2)])
    with pytest.raises(ValueError):
        verify_projection_batch([event] * 1001)


def _ownership_race(path, old, writer_id, ready, start, results):
    journal = RevisionJournal(path)
    ready.put(True)
    if not start.wait(15):
        raise RuntimeError("test barrier timeout")
    try:
        owner = journal.acquire(
            old.scope, writer_id, expected_generation=old.generation
        )
        results.put(("owner", owner))
    except RevisionConflict:
        results.put(("conflict", None))


def test_separate_process_ownership_cas_fences_previous_writer(setup):
    journal, old = setup
    ctx = mp.get_context("spawn")
    ready, results = ctx.Queue(), ctx.Queue()
    start = ctx.Event()
    processes = [
        ctx.Process(
            target=_ownership_race,
            args=(str(journal.path), old, f"new-{i}", ready, start, results),
        )
        for i in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=20)
        start.set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert sorted(kind for kind, _ in outcomes) == ["conflict", "owner"]
        owner = next(value for kind, value in outcomes if kind == "owner")
        assert owner.generation == 2
        with pytest.raises(RevisionConflict, match="stale"):
            write(journal, old)
        assert write(journal, owner).event.generation == 2
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join(5)
        ready.close()
        results.close()
