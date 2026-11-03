from __future__ import annotations

import pytest

from aitos.config.tuning import load_tuning_settings
from aitos.intelligence.candidate_rotation import CandidateRotationManager
from aitos.intelligence.scanner import OpportunityScanner
from tests.test_scanner import FakeScannerExchange


def test_kernel_rejection_cooldown_and_market_change_recheck():
    rotation = CandidateRotationManager(
        default_cooldown_seconds=60,
        kernel_cooldown_seconds=60,
        market_change_recheck_pct=0.15,
    )
    rotation.record_rejection("SOLUSDT", "kernel_rejection", "100:0.001:1000")
    assert rotation.should_skip("SOLUSDT", "100:0.001:1000")
    assert not rotation.should_skip("SOLUSDT", "100.3:0.004:1200")
    assert rotation.snapshot()["early_rechecks"] == 1


def test_portfolio_rejection_does_not_create_symbol_market_cooldown():
    rotation = CandidateRotationManager()
    rotation.record_rejection("ETHUSDT", "portfolio_risk_rejection", "100:0:1000")
    assert not rotation.should_skip("ETHUSDT", "100:0:1000")


def test_zero_duration_cooldown_is_not_a_permanent_blacklist():
    rotation = CandidateRotationManager(
        default_cooldown_seconds=0, kernel_cooldown_seconds=0
    )
    rotation.record_rejection("ADAUSDT", "kernel_rejection", "1:0:100")
    assert not rotation.should_skip("ADAUSDT", "1:0:100")


@pytest.mark.asyncio
async def test_candidate_batches_are_yielded_sequentially(event_bus):
    base = load_tuning_settings(profile="low_resource", mode="paper")
    analysis = base.analysis.model_copy(
        update={
            "candidate_pool_limit": 2,
            "candidate_batch_size": 1,
            "max_candidate_analyses_per_cycle": 2,
            "timeout_seconds": 8,
        }
    )
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=FakeScannerExchange(),
        symbols=["BTCUSDT", "ETHUSDT"],
        reference_symbol="",
        min_score_threshold=0.0,
        top_n=2,
        tuning=base.model_copy(update={"analysis": analysis}),
    )
    await scanner.initialize({})
    batches = []
    async for batch in scanner.iter_candidate_batches():
        batches.append(batch)
    assert len(batches) == 2
    assert any(
        candidate.symbol == "BTCUSDT" for batch in batches for candidate in batch
    )
    await scanner.shutdown()


def test_ranking_hysteresis_retains_incumbent_when_scores_are_close():
    from types import SimpleNamespace
    from aitos.intelligence.opportunity_outcomes import RankingHysteresis

    ranker = RankingHysteresis(margin_points=1.0)
    incumbent = SimpleNamespace(symbol="BTCUSDT", composite_score=70.0)
    challenger = SimpleNamespace(symbol="ETHUSDT", composite_score=70.5)
    assert ranker.rank([incumbent, challenger], 1)[0].symbol == "BTCUSDT"
    assert ranker.rank([incumbent, challenger], 1)[0].symbol == "BTCUSDT"


def test_ranking_hysteresis_allows_clear_challenger():
    from types import SimpleNamespace
    from aitos.intelligence.opportunity_outcomes import RankingHysteresis

    ranker = RankingHysteresis(margin_points=1.0)
    incumbent = SimpleNamespace(symbol="BTCUSDT", composite_score=70.0)
    challenger = SimpleNamespace(symbol="ETHUSDT", composite_score=70.5)
    ranker.rank([incumbent, challenger], 1)
    stronger = SimpleNamespace(symbol="ETHUSDT", composite_score=73.0)
    assert ranker.rank([incumbent, stronger], 1)[0].symbol == "ETHUSDT"


def test_candidate_outcome_store_persists_horizon_markouts(tmp_path):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from aitos.intelligence.opportunity_outcomes import CandidateOutcomeStore

    store = CandidateOutcomeStore(tmp_path / "outcomes.sqlite3")
    candidate = SimpleNamespace(
        symbol="BTCUSDT", direction=SimpleNamespace(value="LONG"),
        entry_price=100.0, composite_score=75.0,
        component_scores={"flow": 8.0}, component_availability={"flow": True},
        regime="trending", rationale=["test"],
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rejection_id = store.record_rejection(candidate, "kernel_rejection", start)
    assert rejection_id
    store.update_price("BTCUSDT", 101.0, start.timestamp() + 60)
    store.update_price("BTCUSDT", 99.0, start.timestamp() + 300)
    store.update_price("BTCUSDT", 102.0, start.timestamp() + 900)
    assert store.snapshot()["markouts_recorded"] == 3
    store.close()
