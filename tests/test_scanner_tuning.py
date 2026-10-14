from __future__ import annotations

import pytest

from aitos.config.tuning import load_tuning_settings
from aitos.intelligence.scanner import OpportunityScanner
from tests.test_scanner import FakeScannerExchange


@pytest.mark.asyncio
async def test_scanner_applies_configured_universe_and_top_n(event_bus):
    base = load_tuning_settings(profile="balanced", mode="paper")
    scanner_tuning = base.scanner.model_copy(
        update={
            "top_n": 2,
            "universe_mode": "allowlist",
            "universe_limit": 1,
            "symbol_allowlist": ("ETHUSDT", "SOLUSDT"),
        }
    )
    tuning = base.model_copy(update={"scanner": scanner_tuning})
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=FakeScannerExchange(),
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        reference_symbol="",
        tuning=tuning,
    )

    assert scanner._symbols == ["ETHUSDT"]
    assert scanner._top_n == 2
    assert (
        scanner._scan_semaphore._value == tuning.analysis.expensive_analysis_concurrency
    )


@pytest.mark.asyncio
async def test_lightweight_screen_respects_expensive_analysis_budget(event_bus):
    base = load_tuning_settings(profile="balanced", mode="paper")
    analysis = base.analysis.model_copy(update={"expensive_analysis_limit": 1})
    tuning = base.model_copy(update={"analysis": analysis})
    scanner = OpportunityScanner(
        event_bus=event_bus,
        exchange=FakeScannerExchange(),
        symbols=["ETHUSDT", "BTCUSDT"],
        reference_symbol="",
        tuning=tuning,
    )

    selected = await scanner._lightweight_rank_symbols()
    assert len(selected) == 1
    assert selected[0] == "BTCUSDT"
