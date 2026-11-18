from datetime import datetime, timezone

from aitos.decision.scanner_bridge import candidate_to_snapshot, decision_event_payload
from aitos.intelligence.scanner import ScanCandidate
from aitos.models.trade import TradeSide


def _candidate():
    return ScanCandidate(
        symbol="BTCUSDT",
        direction=TradeSide.LONG,
        composite_score=82.0,
        component_scores={"order_flow_bias": 8.0},
        component_availability={"order_flow_bias": True},
        rationale=["test"],
        entry_price=90000.0,
        atr=500.0,
        regime="trending",
        scanned_at=datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc).isoformat(),
        candidate_id="candidate-1",
        trace_id="trace-1",
    )


def test_candidate_snapshot_preserves_identity_and_marks_vetoes_false():
    snapshot = candidate_to_snapshot(_candidate())
    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.snapshot_id == "candidate-1"
    assert snapshot.features["composite_score"] == 82.0
    assert snapshot.features["risk_veto"] is False
    assert snapshot.features["execution_veto"] is False
    assert snapshot.features["data_integrity_veto"] is False


def test_shadow_payload_is_never_execution_authority():
    candidate = _candidate()

    class Result:
        decision_id = "decision-1"
        action = type("Action", (), {"value": "ABSTAIN"})()
        proposed_action = type("Action", (), {"value": "LONG"})()
        probabilities = {"LONG": 0.6, "SHORT": 0.2, "NO_TRADE": 0.2}
        raw_probabilities = probabilities
        calibrated = False
        expected_value = None
        evidence_disagreement = None
        reasons = ("calibration cold",)
        model_id = "test"
        model_version = "1"

    payload = decision_event_payload(candidate, Result())
    assert payload["execution_eligible"] is False
    assert payload["shadow_mode"] is True
    assert payload["candidate_id"] == candidate.candidate_id
