import pytest

from aitos.intelligence.scanner import OpportunityScanner, ScanCandidate
from aitos.models.trade import TradeSide


class _Ranker:
    def rank(self, candidates, limit):
        return candidates[:limit]


@pytest.mark.asyncio
async def test_scanner_score_and_top_n_rejections_are_funnel_attributed():
    scanner = object.__new__(OpportunityScanner)
    scanner._min_score_threshold = 60.0
    scanner._top_n = 1
    scanner._ranking_hysteresis = _Ranker()
    events = []

    async def record(**event):
        events.append(event)

    scanner._record_funnel = record
    candidates = [
        ScanCandidate(
            "BTCUSDT", TradeSide.LONG, 80.0, {}, {}, [], 100.0, 1.0, "trending"
        ),
        ScanCandidate(
            "ETHUSDT", TradeSide.SHORT, 70.0, {}, {}, [], 50.0, 1.0, "ranging"
        ),
        ScanCandidate(
            "SOLUSDT", TradeSide.LONG, 55.0, {}, {}, [], 25.0, 1.0, "unknown"
        ),
    ]

    ranked = await scanner.rank(candidates)

    assert [item.symbol for item in ranked] == ["BTCUSDT"]
    indexed = {(item["symbol"], item["stage"]): item for item in events}
    assert indexed[("SOLUSDT", "scanner_score_gate")]["accepted"] is False
    assert indexed[("ETHUSDT", "scanner_score_gate")]["accepted"] is True
    assert indexed[("ETHUSDT", "ranking_gate")]["accepted"] is False
    assert indexed[("BTCUSDT", "ranking_gate")]["accepted"] is True


@pytest.mark.asyncio
async def test_scanner_funnel_persists_active_trace_id():
    from aitos.intelligence import scanner as scanner_module

    class Store:
        def __init__(self):
            self.events = []

        def record(self, **event):
            self.events.append(event)

    instance = object.__new__(OpportunityScanner)
    instance._directional_funnel = Store()
    token = scanner_module._ACTIVE_SCAN_TRACE_ID.set("md:BTCUSDT:event-123")
    try:
        await instance._record_funnel(
            symbol="BTCUSDT",
            side="LONG",
            stage="directional_gate",
            accepted=False,
            reason="cvd_below_directional_threshold",
            details={"cvd_score": 4.0},
        )
    finally:
        scanner_module._ACTIVE_SCAN_TRACE_ID.reset(token)

    assert instance._directional_funnel.events[0]["details"] == {
        "cvd_score": 4.0,
        "trace_id": "md:BTCUSDT:event-123",
    }


def test_scan_trace_id_survives_opportunity_conversion():
    scanner = OpportunityScanner(
        event_bus=None,
        exchange=None,
        symbols=[],
    )
    candidate = ScanCandidate(
        "BTCUSDT",
        TradeSide.LONG,
        80.0,
        {},
        {},
        [],
        100.0,
        1.0,
        "trending",
        trace_id="md:BTCUSDT:event-456",
    )
    opportunity = scanner.to_opportunity(candidate)
    assert opportunity.agent_consensus["trace_id"] == "md:BTCUSDT:event-456"
