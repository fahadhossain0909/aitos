import pytest

from aitos.execution.oms_journal import (
    OMSConflict,
    OMSIntegrityError,
    TransactionalOMSJournal,
)


def test_oms_append_is_cas_ordered_and_hash_chained(tmp_path):
    journal = TransactionalOMSJournal(tmp_path / "oms.sqlite", scope="paper:one")
    fence = journal.acquire_writer("worker-a", expected_generation=0)
    first = journal.append(
        fence,
        aggregate_id="trade-1",
        event_id="evt-1",
        expected_revision=0,
        payload={"state": "OPEN", "qty": "1"},
    )
    second = journal.append(
        fence,
        aggregate_id="trade-1",
        event_id="evt-2",
        expected_revision=1,
        payload={"state": "CLOSED", "qty": "0"},
    )
    assert (first.revision, second.revision) == (1, 2)
    assert [e.payload["state"] for e in journal.read("trade-1")] == ["OPEN", "CLOSED"]


def test_oms_rejects_stale_writer_and_revision(tmp_path):
    journal = TransactionalOMSJournal(tmp_path / "oms.sqlite", scope="paper:one")
    old = journal.acquire_writer("worker-a", expected_generation=0)
    journal.append(
        old, aggregate_id="t", event_id="e1", expected_revision=0, payload={"n": 1}
    )
    new = journal.acquire_writer("worker-b", expected_generation=1)
    with pytest.raises(OMSConflict, match="stale writer"):
        journal.append(
            old, aggregate_id="t", event_id="e2", expected_revision=1, payload={"n": 2}
        )
    with pytest.raises(OMSConflict, match="revision CAS"):
        journal.append(
            new, aggregate_id="t", event_id="e3", expected_revision=0, payload={"n": 3}
        )


def test_oms_idempotent_event_and_reuse_conflict(tmp_path):
    journal = TransactionalOMSJournal(tmp_path / "oms.sqlite", scope="paper:one")
    fence = journal.acquire_writer("worker-a", expected_generation=0)
    a = journal.append(
        fence, aggregate_id="t", event_id="e1", expected_revision=0, payload={"n": 1}
    )
    duplicate = journal.append(
        fence, aggregate_id="t", event_id="e1", expected_revision=0, payload={"n": 1}
    )
    assert duplicate.event_hash == a.event_hash
    with pytest.raises(OMSConflict, match="reused"):
        journal.append(
            fence,
            aggregate_id="t",
            event_id="e1",
            expected_revision=1,
            payload={"n": 2},
        )


def test_oms_scopes_are_isolated(tmp_path):
    path = tmp_path / "oms.sqlite"
    a = TransactionalOMSJournal(path, scope="paper:a")
    b = TransactionalOMSJournal(path, scope="paper:b")
    fa = a.acquire_writer("wa", expected_generation=0)
    fb = b.acquire_writer("wb", expected_generation=0)
    a.append(
        fa, aggregate_id="same", event_id="ea", expected_revision=0, payload={"a": 1}
    )
    b.append(
        fb, aggregate_id="same", event_id="eb", expected_revision=0, payload={"b": 1}
    )
    assert a.read("same")[0].payload == {"a": 1}
    assert b.read("same")[0].payload == {"b": 1}


def test_oms_journal_is_append_only(tmp_path):
    journal = TransactionalOMSJournal(tmp_path / "oms.sqlite", scope="paper:a")
    fence = journal.acquire_writer("w", expected_generation=0)
    journal.append(
        fence, aggregate_id="t", event_id="e", expected_revision=0, payload={"x": 1}
    )
    import sqlite3

    with sqlite3.connect(journal.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM oms_events")
