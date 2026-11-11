"""Typed paper revision binding. All journal files are temporary and synthetic."""

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aitos.core.contracts import Event
from aitos.models.trade import Trade, TradeLifecycleState, TradeSide
from aitos.trading.paper_revision import (
    PaperRevisionStore,
    assess_legacy_rows,
    decode_trade,
    encode_trade,
)
from aitos.trading.revision_journal import (
    JournalIntegrityError,
    RevisionConflict,
    RevisionJournal,
    Scope,
)


def sample_trade():
    return Trade(
        trade_id="synthetic-1",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100,
        quantity=1,
        leverage=1,
        position_size_usd=100,
        risk_amount_usd=2,
        strategy_id="synthetic",
        agent_consensus={},
        explanation="test only",
        sl_price=98,
        tp_price=110,
        state=TradeLifecycleState.POSITION_OPENED,
        entry_time="2026-11-01T00:00:00Z",
        updated_at="2026-11-01T00:00:01Z",
    )


def test_trade_snapshot_keeps_actual_update_timestamp():
    trade = sample_trade()
    assert trade.to_dict()["updated_at"] == trade.updated_at


def make_store(tmp_path):
    journal = RevisionJournal(tmp_path / "typed-paper-only.sqlite")
    scoped = Scope("paper", "synthetic", "paper-account", "run-a")
    writer = journal.acquire(scoped, "writer-a", expected_generation=0)
    return PaperRevisionStore(journal, writer)


def test_complete_model_round_trip_preserves_timestamp_and_types():
    trade = sample_trade()
    restored = decode_trade(encode_trade(trade))
    assert restored == trade
    assert restored.side is TradeSide.LONG
    assert restored.updated_at == trade.updated_at


@pytest.mark.parametrize(
    "key,value",
    [
        ("quantity", True),
        ("quantity", -1),
        ("entry_price", float("nan")),
        ("leverage", "1"),
        ("position_size_usd", 99),
        ("fees_paid_usd", -1),
        ("entry_time", "2026-11-01"),
        ("updated_at", "2026-11-01T00:00:00"),
        ("trailing_sl_enabled", 1),
        ("adaptive_exit_observations", True),
        ("symbol", "lowercase"),
        ("state", "live"),
        ("side", "long"),
        ("pnl", 123),
        ("initial_r_distance", -1),
        ("take_profit_levels", [0]),
        ("partial_exits", [{"price": 100}]),
        ("cost_adjustments", {"x": {"kind": "fee"}}),
        ("entry_thesis", []),
        ("tp_order_ids", [False]),
    ],
)
def test_corrupt_fields_are_not_coerced_or_silently_dropped(key, value):
    data = sample_trade().to_dict()
    data[key] = value
    with pytest.raises(ValueError):
        decode_trade(data)


def test_schema_evolution_requires_explicit_binding():
    for action in ["remove", "add"]:
        data = sample_trade().to_dict()
        if action == "remove":
            data.pop("updated_at")
        else:
            data["unknown_future_field"] = 1
        with pytest.raises(ValueError, match="schema"):
            decode_trade(data)


def test_closed_trade_requires_recorded_fill_and_consistent_net_pnl():
    trade = sample_trade()
    trade.state = TradeLifecycleState.POSITION_CLOSED
    with pytest.raises(ValueError):
        encode_trade(trade)
    trade.exit_price = 110
    trade.exit_time = "2026-11-01T00:01:00Z"
    trade.gross_realized_pnl_usd = 10
    trade.fees_paid_usd = 1
    trade.funding_pnl_usd = -0.5
    trade.pnl = 8.5
    assert decode_trade(encode_trade(trade)) == trade
    trade.pnl = 9
    with pytest.raises(ValueError, match="PnL"):
        encode_trade(trade)


def test_reopen_recovers_latest_quantity_despite_clock_rollback(tmp_path):
    store = make_store(tmp_path)
    trade = sample_trade()
    store.save_trade(trade, event_id="e1", expected_revision=0)
    trade.quantity = 0.5
    trade.position_size_usd = 50
    trade.updated_at = "2026-11-01T00:00:00Z"
    store.save_trade(trade, event_id="e2", expected_revision=1)
    reopened = PaperRevisionStore(RevisionJournal(store.journal.path), store.writer)
    recovered = reopened.recover()
    assert len(recovered.open_trades) == 1
    assert recovered.open_trades[0].quantity == 0.5
    assert recovered.open_trades[0].updated_at == "2026-11-01T00:00:00Z"
    recovered.open_trades[0].quantity = 999
    assert recovered.open_trades[0].quantity == 0.5
    with pytest.raises(RevisionConflict):
        store.save_trade(trade, event_id="e3", expected_revision=1)


def test_recovery_limit_fails_instead_of_partial_financial_state(tmp_path):
    store = make_store(tmp_path)
    for i in range(2):
        trade = replace(sample_trade(), trade_id=f"synthetic-{i}")
        store.save_trade(trade, event_id=f"e{i}", expected_revision=0)
    with pytest.raises(JournalIntegrityError, match="limit"):
        store.recover(limit=1)
    assert len(store.recover().open_trades) == 2


@pytest.mark.asyncio
async def test_monitor_failure_keeps_all_financials_and_attempts_other_monitors(
    tmp_path,
):
    store = make_store(tmp_path)
    for i in range(2):
        store.save_trade(
            replace(sample_trade(), trade_id=f"synthetic-{i}"),
            event_id=f"e{i}",
            expected_revision=0,
        )
    attempts = []

    async def register(trade):
        attempts.append(trade.trade_id)
        if trade.trade_id.endswith("0"):
            raise RuntimeError("synthetic monitor failure")
        return True

    result = await store.rehearse_monitoring(
        register, feed_status={"BTCUSDT": True}, unresolved_intents=0
    )
    assert len(attempts) == 2 and len(result.snapshot.open_trades) == 2
    assert result.failed == ("synthetic-0",) and result.registered == ("synthetic-1",)
    assert not result.new_entries_allowed and not result.live_authorized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feeds,intents,allowed",
    [
        ({}, 0, False),
        ({"BTCUSDT": False}, 0, False),
        ({"BTCUSDT": True}, 1, False),
        ({"BTCUSDT": True}, 0, True),
    ],
)
async def test_paper_readiness_requires_all_prerequisites(
    tmp_path, feeds, intents, allowed
):
    store = make_store(tmp_path)
    store.save_trade(sample_trade(), event_id="e1", expected_revision=0)

    async def register(trade):
        return True

    result = await store.rehearse_monitoring(
        register, feed_status=feeds, unresolved_intents=intents
    )
    assert result.recovery_prerequisites_met is allowed
    assert not result.new_entries_allowed and not result.live_authorized


@pytest.mark.asyncio
async def test_takeover_during_monitor_registration_quarantines_entries(tmp_path):
    store = make_store(tmp_path)
    store.save_trade(sample_trade(), event_id="e1", expected_revision=0)

    async def register(trade):
        store.journal.acquire(store.writer.scope, "new-owner", expected_generation=1)
        return True

    result = await store.rehearse_monitoring(
        register, feed_status={"BTCUSDT": True}, unresolved_intents=0
    )
    assert len(result.snapshot.open_trades) == 1
    assert not result.writer_current and not result.recovery_prerequisites_met
    assert not result.new_entries_allowed


def test_legacy_assessment_does_not_import_infer_history_or_mutate_input(tmp_path):
    store = make_store(tmp_path)
    data = sample_trade().to_dict()
    rows = [
        dict(trade_id=data["trade_id"], updated_at=data["updated_at"], payload=data)
    ]
    second = copy.deepcopy(rows[0])
    second["payload"]["quantity"] = 0.5
    second["payload"]["position_size_usd"] = 50
    rows.append(second)
    before = copy.deepcopy(rows)
    report = assess_legacy_rows(rows, scope=store.writer.scope)
    assert "retained_timestamp_conflict" in report["issues"]
    assert "history_not_attested" in report["issues"]
    assert not report["eligible_to_import"] and rows == before
    assert store.recover().records == ()
    assert "scope_not_attested" in assess_legacy_rows(rows, scope=None)["issues"]
    with pytest.raises(ValueError, match="limit"):
        assess_legacy_rows(rows, scope=None, limit=1)


@pytest.mark.asyncio
async def test_existing_persistence_event_round_trip_keeps_distinct_update_times():
    from aitos.trading.persistent_state import TradeStatePersistence

    saved = []

    class Store:
        async def save_trade(self, trade):
            saved.append(trade)

    persistence = TradeStatePersistence(SimpleNamespace(), SimpleNamespace(), Store())
    first = sample_trade()
    second = replace(
        first, quantity=0.5, position_size_usd=50, updated_at="2026-11-01T00:00:02Z"
    )
    for trade in [first, second]:
        await persistence._save_event(
            Event(
                topic="trade.position_updated",
                payload=trade.to_dict(),
                source_module="isolated-test",
            )
        )
    assert [trade.updated_at for trade in saved] == [
        first.updated_at,
        second.updated_at,
    ]
    assert [trade.quantity for trade in saved] == [1, 0.5]


@pytest.mark.asyncio
async def test_real_simulated_lifecycle_close_after_typed_restart(
    tmp_path, event_bus, risk_engine
):
    from aitos.execution.order_executor import SimulatedOrderExecutor
    from aitos.models.trade import Opportunity
    from aitos.risk.models import PortfolioState
    from aitos.trading.lifecycle import TradeLifecycle

    store = make_store(tmp_path)
    lifecycle = TradeLifecycle(
        event_bus, risk_engine, order_executor=SimulatedOrderExecutor()
    )
    await lifecycle.initialize({})
    opportunity = Opportunity(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100,
        stop_loss_price=98,
        take_profit_levels=[110],
        confidence=0.8,
        strategy_id="synthetic-rehearsal",
        rationale="synthetic data only",
    )
    portfolio = PortfolioState(
        equity_usd=10000,
        peak_equity_usd=10000,
        volatility_percentile=30,
        max_pairwise_correlation=0.1,
    )
    opened = await lifecycle.submit_opportunity(opportunity, portfolio)
    assert opened.state is TradeLifecycleState.POSITION_OPENED
    store.save_trade(opened, event_id="opened", expected_revision=0)
    # New objects and a reopened file model a local restart, not an OS power cut.
    reopened = PaperRevisionStore(RevisionJournal(store.journal.path), store.writer)
    restarted = TradeLifecycle(
        event_bus, risk_engine, order_executor=SimulatedOrderExecutor()
    )
    await restarted.initialize({})
    restored = reopened.recover().open_trades[0]
    restarted._open_trades[restored.trade_id] = (
        restored  # Test-only hydration, no runner wiring.
    )
    closed = await restarted.close_trade(
        restored.trade_id, 110, "synthetic-restart-rehearsal"
    )
    assert closed.state is TradeLifecycleState.POSITION_CLOSED
    reopened.save_trade(closed, event_id="closed", expected_revision=1)
    final = reopened.recover()
    assert final.open_trades == () and len(final.closed_trades) == 1
    assert final.closed_trades[0].pnl == closed.pnl
    assert final.closed_trades[0].partial_exits == closed.partial_exits
    # Original terminal guard stays intact; no implicit financial amendment.
    closed.fees_paid_usd += 1
    closed.pnl -= 1
    with pytest.raises(RevisionConflict, match="terminal"):
        reopened.save_trade(
            closed, event_id="unapproved-amendment", expected_revision=2
        )


@pytest.mark.asyncio
async def test_monitor_cannot_mutate_evidence_to_bypass_missing_feed(tmp_path):
    store = make_store(tmp_path)
    store.save_trade(sample_trade(), event_id="e1", expected_revision=0)

    async def register(trade):
        trade.symbol = "ETHUSDT"
        return True

    result = await store.rehearse_monitoring(
        register, feed_status={"ETHUSDT": True}, unresolved_intents=0
    )
    assert result.failed == ("synthetic-1",)
    assert result.feeds_missing == ("BTCUSDT",)
    assert result.snapshot.open_trades[0].symbol == "BTCUSDT"
    assert not result.new_entries_allowed
