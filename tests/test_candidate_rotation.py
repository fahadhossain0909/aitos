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
