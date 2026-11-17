from __future__ import annotations

import pytest

from aitos.intelligence import scanner as scanner_module


class _Funnel:
    def record(self, **_event):
        return None


class _Ledger:
    def __init__(self):
        self.decision_ids = []

    def record_decision(self, decision_id, **_kwargs):
        self.decision_ids.append(decision_id)
        return True


@pytest.mark.asyncio
async def test_repeated_gate_evaluations_share_entity_but_have_unique_decision_ids():
    instance = scanner_module.OpportunityScanner.__new__(
        scanner_module.OpportunityScanner
    )
    instance._directional_funnel = _Funnel()
    instance._decision_outcome_ledger = _Ledger()

    token = scanner_module._ACTIVE_SCAN_TRACE_ID.set("md:ETHUSDT:replayed-market-event")
    try:
        await instance._record_funnel(
            symbol="ETHUSDT",
            side="LONG",
            stage="directional_gate",
            accepted=False,
            reason="first evaluation",
        )
        await instance._record_funnel(
            symbol="ETHUSDT",
            side="LONG",
            stage="directional_gate",
            accepted=True,
            reason="second evaluation",
        )
    finally:
        scanner_module._ACTIVE_SCAN_TRACE_ID.reset(token)

    assert len(instance._decision_outcome_ledger.decision_ids) == 2
    assert len(set(instance._decision_outcome_ledger.decision_ids)) == 2
    assert all(
        decision_id.startswith(
            "md:ETHUSDT:replayed-market-event:ETHUSDT:LONG:directional_gate:"
        )
        for decision_id in instance._decision_outcome_ledger.decision_ids
    )


class _CandidateOutcomes:
    def __init__(self):
        self.markouts = []

    def record_rejection(self, _candidate, _reason):
        return "rejection-1"

    def completed_markouts(self, *, horizon_seconds):
        assert horizon_seconds == 900
        return self.markouts


class _FinancialML:
    def reject_candidate(self, *_args):
        return True

    def has_candidate(self, _candidate_id):
        # A Financial-ML candidate must not suppress its independent 15m markout.
        return True


class _CandidateRotation:
    def record_rejection(self, *_args):
        return None


class _AttributionLedger:
    def __init__(self):
        self.decisions = []
        self.resolutions = []

    def record_decision(self, decision_id, **kwargs):
        self.decisions.append((decision_id, kwargs))
        return True

    def list_decisions(self, *, entity_id):
        return [row for row in self.resolutions if row["entity_id"] == entity_id]

    def resolve_entity(self, entity_id, **kwargs):
        self.resolutions.append({"entity_id": entity_id, **kwargs})
        return 1


def test_rejected_candidate_markout_has_separate_horizon_entity():
    from types import SimpleNamespace

    from aitos.models.trade import TradeSide

    instance = scanner_module.OpportunityScanner.__new__(
        scanner_module.OpportunityScanner
    )
    instance._financial_ml = _FinancialML()
    instance._candidate_outcomes = _CandidateOutcomes()
    instance._decision_outcome_ledger = _AttributionLedger()
    instance._candidate_rotation = _CandidateRotation()
    instance._lightweight_fingerprints = {}
    candidate = SimpleNamespace(
        candidate_id="candidate-1",
        trace_id="trace-1",
        symbol="BTCUSDT",
        direction=TradeSide.LONG,
        composite_score=75.0,
        regime="trending",
        world_intelligence_shadow={
            "event_ids": ["event-1"],
            "policy": "shadow_only_no_execution_or_sizing_authority",
        },
    )

    instance.record_candidate_rejection(candidate, "risk_gate")
    assert [item[0] for item in instance._decision_outcome_ledger.decisions] == [
        "rejection-1:REJECTION",
        "rejection-1:MARKOUT:900",
    ]
    assert instance._decision_outcome_ledger.decisions[0][1]["payload"][
        "world_intelligence_shadow"
    ]["event_ids"] == ["event-1"]
    assert (
        instance._decision_outcome_ledger.decisions[1][1]["entity_id"]
        == "candidate-1:MARKOUT:900"
    )

    instance._candidate_outcomes.markouts = [
        {
            "attribution_id": "candidate-1",
            "candidate_id": "candidate-1",
            "signed_return_pct": 1.25,
            "observed_at": 1901.0,
            "price": 101.25,
            "mae_pct": 0.1,
            "mfe_pct": 1.4,
            "reason": "risk_gate",
        }
    ]
    assert instance._reconcile_candidate_markouts() == 1
    resolved = instance._decision_outcome_ledger.resolutions[0]
    assert resolved["entity_id"] == "candidate-1:MARKOUT:900"
    assert resolved["outcome_provenance"] == (
        "COUNTERFACTUAL_REJECTED_CANDIDATE_MARKOUT"
    )
