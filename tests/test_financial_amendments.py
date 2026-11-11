"""Temporary-file paper amendments only; no exchange receipt is fetched."""

import hashlib
import multiprocessing as mp
import sqlite3
from dataclasses import replace

import pytest

from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.financial_amendment import AMENDMENT_KEY, CostAmendment
from aitos.trading.paper_revision import PaperRevisionStore, decode_trade
from aitos.trading.revision_journal import (
    JournalIntegrityError,
    RevisionConflict,
    RevisionJournal,
    Scope,
    canonical,
)


def closed(trade_id="trade-1"):
    return Trade(
        trade_id=trade_id,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100,
        quantity=1,
        leverage=1,
        position_size_usd=100,
        risk_amount_usd=2,
        strategy_id="synthetic",
        agent_consensus={},
        explanation="synthetic only",
        sl_price=98,
        tp_price=110,
        state=TradeLifecycleState.POSITION_CLOSED,
        entry_time="2026-11-01T00:00:00Z",
        updated_at="2026-11-01T00:01:00Z",
        exit_price=110,
        exit_time="2026-11-01T00:01:00Z",
        exit_reason="synthetic",
        pnl=10,
        pnl_percent=10,
        gross_realized_pnl_usd=10,
    )


def command(adjustment_id="receipt-1", kind="fee", amount=1):
    return CostAmendment(
        "synthetic-source",
        adjustment_id,
        kind,
        amount,
        "2026-11-01T00:02:00Z",
        hashlib.sha256(f"synthetic:{adjustment_id}".encode()).hexdigest(),
    )


@pytest.fixture
def setup(tmp_path):
    journal = RevisionJournal(tmp_path / "amendments-only.sqlite")
    writer = journal.acquire(
        Scope("paper", "synthetic", "account", "run"), "writer-1", expected_generation=0
    )
    store = PaperRevisionStore(journal, writer)
    parent = store.save_trade(closed(), event_id="close-1", expected_revision=0).event
    return store, parent


def amend(store, parent, cmd=None, event_id="amend-1"):
    return store.amend_cost(
        trade_id=parent.trade_id,
        event_id=event_id,
        expected_revision=parent.revision,
        expected_digest=parent.digest,
        amendment=command() if cmd is None else cmd,
    )


def test_fee_and_signed_funding_preserve_original_close_and_recover(setup):
    store, parent = setup
    original = parent.payload_json
    fee = amend(store, parent).event
    paid = amend(store, fee, command("paid", "funding", -0.5), "amend-2").event
    received = amend(store, paid, command("received", "funding", 0.25), "amend-3").event
    assert received.payload["pnl"] == 8.75
    assert received.payload["pnl_percent"] == 8.75
    assert received.payload["gross_realized_pnl_usd"] == 10
    assert received.payload["fees_paid_usd"] == 1
    assert received.payload["funding_pnl_usd"] == -0.25
    assert len(received.payload["cost_adjustments"]) == 3
    assert parent.payload_json == original
    for key in parent.payload:
        if key not in {
            "fees_paid_usd",
            "funding_pnl_usd",
            "pnl",
            "pnl_percent",
            "updated_at",
            "cost_adjustments",
        }:
            assert received.payload[key] == parent.payload[key]
    with sqlite3.connect(store.journal.path) as db:
        assert (
            db.execute(
                "SELECT payload FROM revision_events WHERE revision=1"
            ).fetchone()[0]
            == original
        )
    reopened = PaperRevisionStore(RevisionJournal(store.journal.path), store.writer)
    assert reopened.recover().open_trades == ()
    assert reopened.recover().closed_trades[0].pnl == 8.75
    from aitos.trading.performance import analyze_closed_trades

    report = analyze_closed_trades(
        [trade.to_dict() for trade in reopened.recover().closed_trades]
    )
    assert report["summary"]["net_pnl_usd"] == 8.75
    with pytest.raises(ValueError, match="duplicate"):
        analyze_closed_trades([parent.payload, fee.payload])
    assert len(reopened.journal.pending(store.writer.scope)) == 4
    assert received.payload[AMENDMENT_KEY]["parent_digest"] == paid.digest


def test_exact_retry_after_newer_amendment_returns_original_ack(setup):
    store, parent = setup
    first = amend(store, parent)
    second = amend(store, first.event, command("receipt-2", "funding", 1), "amend-2")
    retry = amend(store, parent)
    assert (
        retry.duplicate
        and retry.event == first.event
        and retry.head_revision == second.head_revision
    )
    assert store.recover().closed_trades[0].pnl == 10
    with pytest.raises(RevisionConflict):
        amend(store, parent, command(amount=2))
    with pytest.raises(RevisionConflict):
        amend(store, second.event, command(), "different-event")


def test_receipt_cannot_be_charged_twice_under_another_event_or_trade(setup):
    store, parent = setup
    first = amend(store, parent).event
    with pytest.raises(RevisionConflict, match="receipt"):
        amend(store, first, command(), "another-event")
    other = store.save_trade(
        closed("trade-2"), event_id="close-2", expected_revision=0
    ).event
    with pytest.raises(RevisionConflict, match="receipt"):
        amend(store, other, command(), "other-trade-event")
    assert store.journal.head(store.writer.scope, "trade-2").revision == 1


def test_parent_cas_digest_and_writer_generation_are_required(setup):
    store, parent = setup
    with pytest.raises(RevisionConflict, match="parent"):
        amend(store, replace(parent, digest="b" * 64))
    first = amend(store, parent)
    with pytest.raises(RevisionConflict, match="parent"):
        amend(store, parent, command("different"), "different-event")
    store.journal.acquire(store.writer.scope, "writer-2", expected_generation=1)
    with pytest.raises(RevisionConflict, match="stale"):
        amend(store, parent)
    assert (
        store.journal.head(store.writer.scope, parent.trade_id).revision
        == first.head_revision
    )


def test_different_accounts_can_use_same_external_receipt_id(setup):
    store, parent = setup
    amend(store, parent)
    writer = store.journal.acquire(
        replace(store.writer.scope, account="other-account"),
        "writer-b",
        expected_generation=0,
    )
    other = PaperRevisionStore(store.journal, writer)
    base = other.save_trade(closed(), event_id="close-1", expected_revision=0).event
    assert amend(other, base).event.payload["pnl"] == 9


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "rebate"),
        ("amount_usd", True),
        ("amount_usd", float("nan")),
        ("amount_usd", float("inf")),
        ("amount_usd", -1),
        ("recorded_at", "2026-11-01"),
        ("recorded_at", "2026-11-01T00:00:00"),
        ("receipt_sha256", "not-a-digest"),
        ("source", ""),
        ("adjustment_id", "contains space"),
    ],
)
def test_invalid_receipt_contract_rejected(field, value):
    with pytest.raises(ValueError):
        replace(command(), **{field: value})


def test_generic_append_cannot_smuggle_amendment_or_reopen_terminal(setup):
    store, parent = setup
    event = amend(store, parent).event
    with pytest.raises(ValueError, match="explicit"):
        store.journal.append(
            store.writer,
            trade_id=parent.trade_id,
            event_id="smuggled",
            expected_revision=2,
            payload=event.payload,
        )
    changed = decode_trade(event.payload)
    changed.fees_paid_usd += 1
    changed.pnl -= 1
    changed.pnl_percent -= 1
    with pytest.raises(RevisionConflict, match="terminal"):
        store.save_trade(changed, event_id="ordinary-cost-change", expected_revision=2)
    changed.state = TradeLifecycleState.POSITION_OPENED
    with pytest.raises(RevisionConflict, match="terminal"):
        store.save_trade(changed, event_id="reopen", expected_revision=2)


def test_nonterminal_or_existing_legacy_adjustment_is_not_recharged(setup):
    store, _ = setup
    opened = closed("open-trade")
    opened.state = TradeLifecycleState.POSITION_OPENED
    base = store.save_trade(opened, event_id="open", expected_revision=0).event
    with pytest.raises(RevisionConflict, match="closed"):
        amend(store, base, event_id="bad-open-amendment")
    previous = closed("legacy")
    previous.cost_adjustments = {
        "receipt-1": dict(kind="fee", amount_usd=1, recorded_at=command().recorded_at)
    }
    previous.fees_paid_usd = 1
    previous.pnl = 9
    previous.pnl_percent = 9
    base = store.save_trade(
        previous, event_id="legacy-close", expected_revision=0
    ).event
    with pytest.raises(RevisionConflict, match="new receipt"):
        amend(store, base, event_id="legacy-duplicate")


def test_outbox_failure_rolls_back_all_amendment_changes(setup):
    store, parent = setup
    store.journal.acknowledge(store.writer, parent)
    with sqlite3.connect(store.journal.path) as db:
        db.execute(
            "CREATE TRIGGER abort_amendment BEFORE INSERT ON revision_outbox BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        amend(store, parent)
    assert store.journal.head(store.writer.scope, parent.trade_id) == parent
    assert store.journal.pending(store.writer.scope) == []
    assert store.recover().closed_trades[0].fees_paid_usd == 0


def test_lineage_corruption_is_not_accepted_even_with_recomputed_payload_hash(setup):
    store, parent = setup
    amended = amend(store, parent).event
    payload = amended.payload
    payload[AMENDMENT_KEY]["parent_digest"] = "b" * 64
    text = canonical(payload)
    with sqlite3.connect(store.journal.path) as db:
        db.execute(
            "UPDATE revision_events SET payload=?,digest=? WHERE revision=2",
            (text, hashlib.sha256(text.encode()).hexdigest()),
        )
    with pytest.raises(JournalIntegrityError, match="lineage"):
        store.recover()


def _race(path, writer, parent, same, number, ready, start, results):
    store = PaperRevisionStore(RevisionJournal(path), writer)
    ready.put(True)
    if not start.wait(15):
        raise RuntimeError("test barrier timeout")
    try:
        result = amend(
            store,
            parent,
            command("receipt-1" if same else f"receipt-{number}"),
            "same-event" if same else f"event-{number}",
        )
        results.put("duplicate" if result.duplicate else "committed")
    except RevisionConflict:
        results.put("conflict")


@pytest.mark.parametrize("same", [True, False])
def test_separate_process_amendments_cannot_double_charge_parent(setup, same):
    store, parent = setup
    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    results = ctx.Queue()
    start = ctx.Event()
    processes = [
        ctx.Process(
            target=_race,
            args=(
                str(store.journal.path),
                store.writer,
                parent,
                same,
                i,
                ready,
                start,
                results,
            ),
        )
        for i in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=20)
        start.set()
        outcomes = sorted(results.get(timeout=20) for _ in processes)
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        assert outcomes == (
            ["committed", "duplicate"] if same else ["committed", "conflict"]
        )
        assert store.journal.head(store.writer.scope, parent.trade_id).revision == 2
        assert store.recover().closed_trades[0].pnl == 9
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join(5)
        ready.close()
        results.close()


def test_receipt_digest_cannot_be_relabelled_to_charge_again(setup):
    store, parent = setup
    cmd = command()
    amended = amend(store, parent, cmd).event
    with pytest.raises(RevisionConflict, match="receipt"):
        amend(store, amended, replace(cmd, adjustment_id="new-label"), "new-event")
    assert store.recover().closed_trades[0].pnl == 9


def test_arithmetic_overflow_does_not_commit(setup):
    store, parent = setup
    large = amend(
        store, parent, command("large-1", amount=1e308), "large-event-1"
    ).event
    with pytest.raises(RevisionConflict):
        amend(store, large, command("large-2", amount=1e308), "large-event-2")
    assert store.journal.head(store.writer.scope, parent.trade_id) == large


@pytest.mark.asyncio
async def test_calculation_matches_existing_simulated_lifecycle(
    setup, event_bus, risk_engine, monkeypatch
):
    from aitos.trading.lifecycle import TradeLifecycle

    lifecycle = TradeLifecycle(event_bus, risk_engine)
    await lifecycle.initialize({})
    lifecycle._closed_trades.append(closed())
    store, parent = setup
    for i, cmd in enumerate(
        [
            command("fee"),
            command("funding-paid", "funding", -0.5),
            command("funding-received", "funding", 0.25),
        ]
    ):
        monkeypatch.setattr(
            "aitos.trading.lifecycle.utc_now_iso", lambda: cmd.recorded_at
        )
        actual = await lifecycle.apply_cost_adjustment(
            parent.trade_id,
            adjustment_id=cmd.adjustment_id,
            kind=cmd.kind,
            amount_usd=cmd.amount_usd,
        )
        event = amend(store, parent, cmd, f"compat-{i}").event
        expected = decode_trade(event.payload)
        assert canonical(expected.to_dict()) == canonical(actual.to_dict())
        parent = event


def test_receipt_digest_is_unique_even_if_source_label_changes(setup):
    store, parent = setup
    cmd = command()
    event = amend(store, parent, cmd).event
    with pytest.raises(RevisionConflict, match="receipt"):
        amend(
            store,
            event,
            replace(cmd, source="relabelled-source", adjustment_id="new-id"),
            "source-relabel",
        )


@pytest.mark.parametrize("amount", [0, 0.00000001])
def test_zero_and_small_fee_receipts_are_not_rounded_away_from_the_ledger(
    setup, amount
):
    store, parent = setup
    event = amend(store, parent, command(amount=amount)).event
    assert event.payload["fees_paid_usd"] == amount
    assert event.payload["cost_adjustments"]["receipt-1"]["amount_usd"] == amount
    assert event.payload["pnl"] == round(10 - amount, 8)


def _crash_amendment(path, writer, parent, before_commit):
    import os
    from contextlib import contextmanager

    journal = RevisionJournal(path)
    if before_commit:
        original = journal._connect

        @contextmanager
        def connection():
            with original() as db:
                db.create_function("test_amendment_exit", 0, lambda: os._exit(23))
                yield db

        journal._connect = connection
    amend(PaperRevisionStore(journal, writer), parent)
    os._exit(24)


@pytest.mark.parametrize("before_commit", [True, False])
def test_actual_process_exit_does_not_lose_or_double_apply_cost(setup, before_commit):
    store, parent = setup
    if before_commit:
        with sqlite3.connect(store.journal.path) as db:
            db.execute(
                "CREATE TRIGGER amendment_exit BEFORE INSERT ON revision_outbox BEGIN SELECT test_amendment_exit(); END"
            )
    process = mp.get_context("spawn").Process(
        target=_crash_amendment,
        args=(str(store.journal.path), store.writer, parent, before_commit),
    )
    try:
        process.start()
        process.join(20)
        assert process.exitcode == (23 if before_commit else 24)
        reopened = PaperRevisionStore(RevisionJournal(store.journal.path), store.writer)
        if before_commit:
            assert reopened.journal.head(store.writer.scope, parent.trade_id) == parent
            assert reopened.recover().closed_trades[0].pnl == 10
            assert len(reopened.journal.pending(store.writer.scope)) == 1
        else:
            assert reopened.recover().closed_trades[0].pnl == 9
            assert amend(reopened, parent).duplicate
            assert len(reopened.journal.pending(store.writer.scope)) == 2
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)


def test_scope_recovery_rejects_duplicate_receipt_digest_in_other_trade(setup):
    store, parent = setup
    first = amend(store, parent).event
    other = store.save_trade(
        closed("other-trade"), event_id="other-close", expected_revision=0
    ).event
    second = amend(store, other, command("other-receipt"), "other-amend").event
    payload = second.payload
    payload[AMENDMENT_KEY]["receipt_sha256"] = first.payload[AMENDMENT_KEY][
        "receipt_sha256"
    ]
    text = canonical(payload)
    with sqlite3.connect(store.journal.path) as db:
        db.execute(
            "UPDATE revision_events SET payload=?,digest=? WHERE trade_id=? AND revision=?",
            (
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                second.trade_id,
                second.revision,
            ),
        )
    with pytest.raises(JournalIntegrityError, match="duplicate authoritative"):
        store.recover()
    with pytest.raises(JournalIntegrityError, match="duplicate authoritative"):
        store.journal.pending(store.writer.scope)


def test_amendment_cannot_rewrite_gross_pnl_even_with_self_consistent_net(setup):
    store, parent = setup
    event = amend(store, parent).event
    payload = event.payload
    payload.update(gross_realized_pnl_usd=11, pnl=10, pnl_percent=10)
    text = canonical(payload)
    with sqlite3.connect(store.journal.path) as db:
        db.execute(
            "UPDATE revision_events SET payload=?,digest=? WHERE revision=2",
            (text, hashlib.sha256(text.encode()).hexdigest()),
        )
    with pytest.raises(JournalIntegrityError, match="lineage"):
        store.recover()


def test_invalid_parent_cannot_be_washed_into_a_valid_amended_head(setup):
    store, parent = setup
    amended = amend(store, parent).event
    invalid_parent = parent.payload
    invalid_parent.update(fees_paid_usd=-1, pnl=11, pnl_percent=11)
    old_text = canonical(invalid_parent)
    old_digest = hashlib.sha256(old_text.encode()).hexdigest()
    apparently_valid = amended.payload
    apparently_valid.update(fees_paid_usd=0, pnl=10, pnl_percent=10)
    apparently_valid[AMENDMENT_KEY]["parent_digest"] = old_digest
    new_text = canonical(apparently_valid)
    with sqlite3.connect(store.journal.path) as db:
        db.execute(
            "UPDATE revision_events SET payload=?,digest=? WHERE revision=1",
            (old_text, old_digest),
        )
        db.execute(
            "UPDATE revision_events SET payload=?,digest=? WHERE revision=2",
            (new_text, hashlib.sha256(new_text.encode()).hexdigest()),
        )
    with pytest.raises(JournalIntegrityError, match="lineage"):
        store.recover()
