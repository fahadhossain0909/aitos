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
    assert scanner._scan_semaphore._value == tuning.analysis.expensive_analysis_concurrency
    await scanner.shutdown()
