from types import SimpleNamespace

import pytest

from aitos.intelligence.opportunity_outcomes import CandidateOutcomeStore
from aitos.models.trade import TradeSide
from aitos.statistics.decision_ledger import DecisionOutcomeLedger


def test_rejected_candidate_15m_markout_resolves_trace_linked_decisions(tmp_path):
    candidate_store = CandidateOutcomeStore(tmp_path / "candidate-outcomes.sqlite3")
    ledger = DecisionOutcomeLedger(tmp_path / "decision-outcomes.sqlite3")
    candidate = SimpleNamespace(
        entry_price=100.0,
        direction=TradeSide.LONG,
        component_scores={"flow": 8.0},
        component_availability={"flow": True},
        regime="trending",
        rationale=["flow confirmation"],
        symbol="BTCUSDT",
        composite_score=82.0,
        trace_id="scan-abc",
    )
    rejection_id = candidate_store.record_rejection(
        candidate, "risk_gate", timestamp=1000
    )
    attribution_id = f"{candidate.trace_id}:{candidate.direction.value}"
    ledger.record_decision(
        "scan-abc:BTCUSDT:LONG:liquidity_gate",
        entity_id=attribution_id,
        symbol="BTCUSDT",
        side="LONG",
        decision_type="GATE",
        accepted=False,
        reason="liquidity_gate",
        decision_at="2026-11-07T00:00:00+00:00",
    )
    ledger.record_decision(
        f"{rejection_id}:REJECTION",
        entity_id=attribution_id,
        symbol="BTCUSDT",
        side="LONG",
        decision_type="REJECTION",
        accepted=False,
        reason="risk_gate",
        decision_at="2026-11-07T00:00:00+00:00",
    )

    candidate_store.update_price("BTCUSDT", 101.0, timestamp=1901)
    markouts = candidate_store.completed_markouts(horizon_seconds=900)
    assert len(markouts) == 1
    assert markouts[0]["attribution_id"] == attribution_id
    assert markouts[0]["signed_return_pct"] == pytest.approx(1.0)

    assert (
        ledger.resolve_entity(
            attribution_id,
            outcome_value=markouts[0]["signed_return_pct"] / 100.0,
            outcome_at="2026-11-07T00:15:01+00:00",
            outcome={"counterfactual": True, "horizon_seconds": 900},
        )
        == 2
    )
    rows = ledger.list_decisions(entity_id=attribution_id)
    assert len(rows) == 2
    assert all(row["outcome"]["counterfactual"] for row in rows)


def test_rejected_candidate_records_all_forward_markout_horizons(tmp_path):
    store = CandidateOutcomeStore(tmp_path / "candidate-outcomes.sqlite3")
    candidate = SimpleNamespace(
        entry_price=100.0,
        direction=TradeSide.SHORT,
        component_scores={"flow": -4.0},
        component_availability={"flow": True},
        regime="volatile",
        rationale=["sell pressure"],
        symbol="ETHUSDT",
        composite_score=61.0,
        trace_id="scan-xyz",
    )
    rejection_id = store.record_rejection(candidate, "entry_gate", timestamp=1000)
    store.update_price("ETHUSDT", 99.0, timestamp=1901)
    markouts = store.completed_markouts(rejection_id=rejection_id)
    assert [row["horizon_seconds"] for row in markouts] == [60, 300, 900]
    assert all(row["signed_return_pct"] == pytest.approx(1.0) for row in markouts)
