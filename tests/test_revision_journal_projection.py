"""Optional test-only real append-only evidence projection; never external DB."""

import json
from dataclasses import replace
import pytest
from aitos.trading.revision_journal import (
    JournalIntegrityError,
    PROJECTION_DDL,
    Revision,
    RevisionJournal,
    Scope,
    verify_projection_batch,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def projection(tmp_path):
    pytest.importorskip("chdb")
    from chdb.session import Session

    journal = RevisionJournal(tmp_path / "unwired-only.sqlite")
    scope = Scope("paper", "synthetic", "account", "run")
    writer = journal.acquire(scope, "writer", expected_generation=0)
    event = journal.append(
        writer,
        trade_id="one",
        event_id="event-1",
        expected_revision=0,
        payload={"trade_id": "one", "state": "position_opened", "quantity": 1},
    ).event
    engine = Session(str(tmp_path / "isolated-projection"))
    engine.query(PROJECTION_DDL)
    try:
        yield engine, journal, scope, writer, event
    finally:
        engine.close()


def insert(engine, event):
    engine.query(
        "INSERT INTO prototype_revision_evidence FORMAT JSONEachRow\n"
        + json.dumps(event.__dict__)
    )


def read(engine):
    rows = json.loads(
        str(
            engine.query(
                "SELECT scope_key,trade_id,revision,event_id,writer_id,generation,payload_json,digest FROM prototype_revision_evidence ORDER BY revision",
                "JSONCompact",
            )
        )
    )["data"]
    return [Revision(*row) for row in rows]


def test_exact_duplicate_projection_retained_verified_then_acknowledged(projection):
    engine, journal, scope, writer, event = projection
    insert(engine, event)  # Sink succeeds; simulate lost ACK/restart.
    reopened = RevisionJournal(journal.path)
    insert(engine, reopened.pending(scope)[0])
    engine.query("OPTIMIZE TABLE prototype_revision_evidence FINAL")
    actual = read(engine)
    assert len(actual) == 2  # MergeTree never silently replaces duplicate evidence.
    assert verify_projection_batch(actual) == [event]
    reopened.acknowledge(writer, event)
    assert reopened.pending(scope) == []


def test_conflicting_projection_retained_and_blocks_ack(projection):
    engine, journal, scope, writer, event = projection
    insert(engine, event)
    # A structurally valid but inconsistent writer envelope must not be deduped.
    insert(engine, replace(event, writer_id="different-writer"))
    engine.query("OPTIMIZE TABLE prototype_revision_evidence FINAL")
    assert len(read(engine)) == 2
    with pytest.raises(JournalIntegrityError, match="conflicting"):
        verify_projection_batch(read(engine))
    assert len(journal.pending(scope)) == 1  # No local ACK after sink conflict.


def test_financial_amendment_projection_preserves_original_close(projection, tmp_path):
    from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
    from aitos.trading.financial_amendment import CostAmendment, AMENDMENT_KEY
    from aitos.trading.paper_revision import PaperRevisionStore, decode_trade

    engine, _, scope, _, _ = projection
    journal = RevisionJournal(tmp_path / "typed-financial-projection.sqlite")
    writer = journal.acquire(scope, "financial-writer", expected_generation=0)
    store = PaperRevisionStore(journal, writer)
    trade = Trade(
        trade_id="closed-one",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100,
        quantity=1,
        leverage=1,
        position_size_usd=100,
        risk_amount_usd=2,
        strategy_id="synthetic",
        agent_consensus={},
        explanation="not real funds",
        sl_price=98,
        tp_price=110,
        state=TradeLifecycleState.POSITION_CLOSED,
        entry_time="2026-11-01T00:00:00Z",
        updated_at="2026-11-01T00:01:00Z",
        exit_time="2026-11-01T00:01:00Z",
        exit_price=110,
        pnl=10,
        pnl_percent=10,
        gross_realized_pnl_usd=10,
    )
    base = store.save_trade(trade, event_id="closed", expected_revision=0).event
    updated = store.amend_cost(
        trade_id=trade.trade_id,
        event_id="fee",
        expected_revision=1,
        expected_digest=base.digest,
        amendment=CostAmendment(
            "synthetic", "receipt-1", "fee", 1, "2026-11-01T00:02:00Z", "a" * 64
        ),
    ).event
    for event in [base, updated, updated]:
        insert(engine, event)
    engine.query("OPTIMIZE TABLE prototype_revision_evidence FINAL")
    records = read(engine)
    assert len(records) == 3
    unique = verify_projection_batch(records)
    assert unique == [base, updated]
    assert base.payload["pnl"] == 10 and updated.payload["pnl"] == 9
    assert updated.payload[AMENDMENT_KEY]["parent_digest"] == base.digest
    assert decode_trade(updated.payload).pnl == 9
    for event in unique:
        journal.acknowledge(writer, event)
    assert journal.pending(scope) == []
