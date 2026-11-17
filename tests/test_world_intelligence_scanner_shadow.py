from types import SimpleNamespace

from aitos.intelligence.scanner import OpportunityScanner, ScanCandidate
from aitos.models.trade import TradeSide


def test_scanner_carries_world_intelligence_as_shadow_metadata_only():
    scanner = OpportunityScanner.__new__(OpportunityScanner)
    scanner._financial_ml = SimpleNamespace(mode="shadow")
    scanner._timeframe = "15m"
    candidate = ScanCandidate(
        symbol="BTCUSDT",
        direction=TradeSide.LONG,
        composite_score=75.0,
        component_scores={"order_flow_bias": 8.0},
        component_availability={"order_flow_bias": True},
        rationale=["order flow aligned"],
        entry_price=100.0,
        atr=1.0,
        regime="trending",
        world_intelligence_shadow={
            "event_count": 3,
            "attention_score": 0.7,
            "policy": "shadow_only_no_execution_or_sizing_authority",
        },
    )
    original_scores = dict(candidate.component_scores)
    opportunity = scanner.to_opportunity(candidate)
    assert opportunity.agent_consensus["world_intelligence_shadow"]["event_count"] == 3
    assert candidate.component_scores == original_scores
    assert "world_intelligence_shadow" not in candidate.component_scores
