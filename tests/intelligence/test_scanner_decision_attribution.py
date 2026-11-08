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

    token = scanner_module._ACTIVE_SCAN_TRACE_ID.set(
        "md:ETHUSDT:replayed-market-event"
    )
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
