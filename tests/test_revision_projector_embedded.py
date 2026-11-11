"""Real bounded projector SQL against temporary embedded ClickHouse, no network."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aitos.trading.revision_journal import PROJECTION_DDL, RevisionJournal, Scope
from aitos.trading.revision_projector import ClickHouseEvidenceSink, RevisionProjector

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(tmp_path):
    pytest.importorskip("chdb")
    from chdb.session import Session

    engine = Session(str(tmp_path / "projector-embedded-only"))
    engine.query(PROJECTION_DDL)

    class Client:
        async def query(self, sql, parameters=None):
            data = json.loads(str(engine.query(sql, "JSONCompact", params=parameters)))
            return SimpleNamespace(result_rows=data["data"])

        async def insert(self, table, rows, column_names):
            assert table == "prototype_revision_evidence"
            body = "\n".join(json.dumps(dict(zip(column_names, row))) for row in rows)
            engine.query(
                "INSERT INTO prototype_revision_evidence FORMAT JSONEachRow\n" + body
            )

    journal = RevisionJournal(tmp_path / "local-journal.sqlite")
    writer = journal.acquire(
        Scope("paper", "synthetic", "account", "run"), "writer", expected_generation=0
    )
    events = [
        journal.append(
            writer,
            trade_id="one",
            event_id=f"e{i}",
            expected_revision=i,
            payload={"trade_id": "one", "state": "position_opened", "quantity": 1},
        ).event
        for i in range(2)
    ]
    try:
        yield engine, journal, writer, events, ClickHouseEvidenceSink(Client())
    finally:
        engine.close()


async def test_real_projector_parameterized_lookup_insert_and_readback(setup):
    engine, journal, writer, events, sink = setup
    assert await sink.lookup(events) == []
    result = await RevisionProjector(journal, writer, sink).run_once()
    assert result["status"] == "verified", result
    assert result["counts"] == dict(total=2, pending=0, delivered=2)
    assert await sink.lookup(events) == events
    assert (
        str(
            engine.query("SELECT count() FROM prototype_revision_evidence", "CSV")
        ).strip()
        == "2"
    )


async def test_preexisting_sink_event_id_conflict_is_detected_even_at_other_revision(
    setup,
):
    _, journal, writer, events, sink = setup
    await sink.insert([replace(events[0], revision=99)])
    result = await RevisionProjector(journal, writer, sink).run_once()
    assert result["status"] == "conflict"
    assert journal.outbox_counts(writer.scope)["pending"] == 2


async def test_lost_ack_reopen_does_not_reinsert_existing_real_rows(setup, monkeypatch):
    engine, journal, writer, _, sink = setup
    original = journal.acknowledge_batch

    def lost_ack(*args):
        raise RuntimeError("test-only crash before local acknowledgement")

    monkeypatch.setattr(journal, "acknowledge_batch", lost_ack)
    assert (await RevisionProjector(journal, writer, sink).run_once())[
        "status"
    ] == "failed"
    monkeypatch.setattr(journal, "acknowledge_batch", original)
    reopened = RevisionJournal(journal.path)
    result = await RevisionProjector(reopened, writer, sink).run_once()
    assert result["last_batch"]["inserted"] == 0
    assert result["counts"]["delivered"] == 2
    assert (
        str(
            engine.query("SELECT count() FROM prototype_revision_evidence", "CSV")
        ).strip()
        == "2"
    )
