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
        == 7
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


def test_decision_ledger_persists_candidate_and_resolution_provenance(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "candidate-decisions.sqlite3")
    candidate_id = "a" * 32
    assert ledger.record_decision(
        "candidate-entry",
        entity_id=candidate_id,
        symbol="BTCUSDT",
        side="LONG",
        decision_type="ENTRY",
        accepted=True,
        reason="paper fill",
        payload={"candidate_id": candidate_id, "trace_id": "trace-1"},
        candidate_id=candidate_id,
    )
    assert (
        ledger.resolve_entity(
            candidate_id,
            outcome_value=0.012,
            outcome_at="2026-11-08T12:00:00+00:00",
            outcome={"provenance": "EXECUTED_TRADE_NET_OUTCOME"},
            candidate_id=candidate_id,
            outcome_provenance="EXECUTED_TRADE_NET_OUTCOME",
        )
        == 1
    )
    row = ledger.list_decisions(entity_id=candidate_id)[0]
    assert row["candidate_id"] == candidate_id
    assert row["label_resolution_time"] == "2026-11-08T12:00:00+00:00"
    assert row["outcome_provenance"] == "EXECUTED_TRADE_NET_OUTCOME"


def test_decision_ledger_migrates_legacy_schema_without_data_loss(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE decision_outcomes (
            decision_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL,
            symbol TEXT NOT NULL, side TEXT NOT NULL, decision_type TEXT NOT NULL,
            decision_at TEXT NOT NULL, accepted INTEGER NOT NULL, reason TEXT NOT NULL,
            payload_json TEXT NOT NULL, outcome_value REAL, outcome_at TEXT,
            outcome_json TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO decision_outcomes
        (decision_id,entity_id,symbol,side,decision_type,decision_at,accepted,
         reason,payload_json) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "legacy-1",
            "legacy-entity",
            "ETHUSDT",
            "SHORT",
            "REJECTION",
            "2026-11-01T00:00:00Z",
            0,
            "legacy reason",
            "{}",
        ),
    )
    connection.commit()
    connection.close()

    ledger = DecisionOutcomeLedger(path)
    row = ledger.list_decisions(entity_id="legacy-entity")[0]
    assert row["decision_id"] == "legacy-1"
    assert row["candidate_id"] is None
    assert row["label_resolution_time"] is None


def test_decision_ledger_snapshot_reports_resolution_and_provenance_counts(tmp_path):
    ledger = DecisionOutcomeLedger(tmp_path / "snapshot.sqlite3")
    for decision_id, kind in (("gate-1", "GATE"), ("reject-1", "REJECTION")):
        ledger.record_decision(
            decision_id,
            entity_id="candidate-1",
            symbol="BTCUSDT",
            side="LONG",
            decision_type=kind,
            accepted=False,
            reason="test",
            candidate_id="candidate-1",
        )
    ledger.resolve_entity(
        "candidate-1",
        outcome_value=-0.002,
        outcome_at="2026-11-08T12:15:00+00:00",
        outcome={"counterfactual": True},
        candidate_id="candidate-1",
        outcome_provenance="COUNTERFACTUAL_REJECTED_CANDIDATE_MARKOUT",
    )
    snapshot = ledger.snapshot()
    assert snapshot["decisions_total"] == 2
    assert snapshot["resolved_total"] == 2
    assert snapshot["unresolved_total"] == 0
    assert snapshot["by_type"]["GATE"] == {"count": 1, "resolved": 1}
    assert snapshot["by_type"]["REJECTION"] == {"count": 1, "resolved": 1}
    assert snapshot["by_provenance"] == {
        "COUNTERFACTUAL_REJECTED_CANDIDATE_MARKOUT": 2
    }
