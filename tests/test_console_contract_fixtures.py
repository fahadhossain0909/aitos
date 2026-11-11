"""Keep browser contract fixtures synchronized with the real Python producers."""

import json
from pathlib import Path
from aitos.trading.performance import analyze_closed_trades
from aitos.learning.evaluation_plan import EvaluationPlanRequest, build_evaluation_plan

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_performance_fixture_is_actual_backend_contract():
    expected = json.loads((ROOT / "desktop/src/performance.fixture.json").read_text())
    row = dict(
        trade_id="contract-test-1",
        symbol="BTCUSDT",
        side="LONG",
        state="position_closed",
        pnl=10,
        entry_time="2026-11-01T00:00:00Z",
        exit_time="2026-11-02T00:00:00Z",
        strategy_id="offline-contract-fixture",
        fees_paid_usd=1,
        funding_pnl_usd=0,
    )
    actual = analyze_closed_trades([row])
    actual["generated_at"] = "2026-11-07T12:00:00Z"
    assert actual == expected


def test_frontend_research_fixture_is_actual_backend_contract():
    expected = json.loads((ROOT / "desktop/src/research.fixture.json").read_text())
    assert (
        build_evaluation_plan(EvaluationPlanRequest("2025-01-01", "2026-11-07"))
        == expected
    )


def test_watermarked_browser_performance_fixture_is_actual_backend_contract():
    rows = [
        dict(
            trade_id=f"browser-fixture-{i}",
            symbol="BTCUSDT" if i % 2 else "ETHUSDT",
            side="LONG" if i % 2 else "SHORT",
            state="position_closed",
            pnl=p,
            entry_time="2026-10-01T00:00:00Z",
            exit_time=f"2026-11-{i+1:02}T12:00:00Z",
            strategy_id="BROWSER-TEST-FIXTURE",
            fees_paid_usd=1,
            funding_pnl_usd=-0.2,
        )
        for i, p in enumerate([120, -30, 90, -70, 50, -10])
    ]
    assert analyze_closed_trades(rows) == json.loads(
        (ROOT / "desktop/fixtures/performance-browser.json").read_text()
    )
