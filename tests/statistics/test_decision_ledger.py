import pytest

from aitos.statistics.decision_ledger import DecisionOutcomeLedger


def test_decision_ledger_links_gate_entry_hold_exit_to_net_outcome(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "decisions.sqlite3")
    entity = "scan-trace-123"
    for decision_id, kind, accepted in (
        ("gate-1", "GATE", False),
        ("entry-1", "ENTRY", True),
        ("hold-1", "HOLD", False),
        ("manage-1", "MANAGE", True),
        ("exit-1", "EXIT", True),
        ("execution-failure-1", "EXECUTION_FAILURE", False),
        ("rejection-1", "REJECTION", False),
    ):
        assert ledger.record_decision(
            decision_id,
            entity_id=entity,
            symbol="BTCUSDT",
            side="LONG",
            decision_type=kind,
            accepted=accepted,
            reason=f"{kind.lower()} reason",
            payload={"trace_id": entity},
            decision_at="2026-11-07T10:00:00+00:00",
        )
    assert not ledger.record_decision(
        "entry-1",
        entity_id=entity,
        symbol="BTCUSDT",
        side="LONG",
        decision_type="ENTRY",
        accepted=True,
        reason="entry reason",
        payload={"trace_id": entity},
        decision_at="2026-11-07T10:00:00+00:00",
    )
    assert (
        ledger.resolve_entity(
            entity,
            outcome_value=0.012,
            outcome_at="2026-11-07T11:00:00+00:00",
            outcome={"net_pnl_usd": 1.2},
        )
        == 4
    )
    rows = ledger.list_decisions(entity_id=entity)
    assert len(rows) == 7
    assert all(row["outcome_value"] == pytest.approx(0.012) for row in rows)
    assert ledger.list_decisions(entity_id=entity, unresolved_only=True) == ()


def test_decision_ledger_detects_conflicting_ids_and_outcomes(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "decisions.sqlite3")
    args = dict(
        entity_id="trade-1",
        symbol="ETHUSDT",
        side="SHORT",
        decision_type="REJECTION",
        accepted=False,
        reason="risk veto",
    )
    ledger.record_decision("decision-1", **args)
    with pytest.raises(ValueError):
        ledger.record_decision("decision-1", **{**args, "reason": "different"})
    ledger.resolve_entity(
        "trade-1", outcome_value=-0.01, outcome_at="2026-11-07T11:00:00Z"
    )
    with pytest.raises(ValueError):
        ledger.resolve_entity(
            "trade-1", outcome_value=0.01, outcome_at="2026-11-07T12:00:00Z"
        )


def test_decision_ledger_validates_types_and_limits(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "decisions.sqlite3")
    with pytest.raises(ValueError):
        ledger.record_decision(
            "x",
            entity_id="e",
            symbol="BTC",
            side="LONG",
            decision_type="UNKNOWN",
            accepted=False,
            reason="bad",
        )
    with pytest.raises(ValueError):
        ledger.list_decisions(limit=0)
