from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aitos.intelligence.scanner import _measure_event_loop_lag

from aitos.intelligence.amt.engine import AMTEngine
from aitos.intelligence.footprint import FootprintEngine
from aitos.intelligence.footprint_signals import FootprintSignalEngine
from aitos.intelligence.scanner_cpu import (
    compute_amt_footprint_features,
    get_scanner_cpu_executor,
    run_scanner_cpu,
)
from aitos.models.market import TradeSide, TradeTick


def _trades() -> list[TradeTick]:
    start = datetime.now(timezone.utc) - timedelta(seconds=30)
    return [
        TradeTick(
            symbol="BTCUSDT",
            trade_id=index,
            price=90000.0 + (index % 5) * 0.1,
            quantity=0.01 + (index % 3) * 0.005,
            side=TradeSide.BUY if index % 2 else TradeSide.SELL,
            is_buyer_maker=index % 2 == 0,
            timestamp=start + timedelta(milliseconds=index * 100),
        )
        for index in range(30)
    ]


def test_isolated_feature_function_preserves_amt_and_footprint_outputs():
    trades = _trades()
    expected_amt = AMTEngine(0.1, value_area_pct=0.70, ib_minutes=60).analyze(
        trades, klines=[], book=None, previous_profile=None
    )
    footprint = FootprintEngine(0.1).build(trades)
    expected_signals = FootprintSignalEngine(
        imbalance_threshold=0.60, min_level_volume=0.0
    ).evaluate(footprint)

    actual = compute_amt_footprint_features(
        trades, [], None, None, 0.1, 0.70, 60, 0.60, 0.0
    )

    assert actual == (expected_amt, expected_signals)
    assert actual[0] is not None
    assert actual[1] is not None


@pytest.mark.asyncio
async def test_spawned_cpu_worker_matches_inline_feature_results():
    trades = _trades()
    args = (trades, [], None, None, 0.1, 0.70, 60, 0.60, 0.0)
    expected = compute_amt_footprint_features(*args)

    actual = await run_scanner_cpu(compute_amt_footprint_features, *args)

    assert actual == expected


def test_scanner_cpu_executor_is_bounded_to_one_spawned_worker():
    assert get_scanner_cpu_executor()._max_workers == 1


@pytest.mark.asyncio
async def test_event_loop_lag_probe_detects_synchronous_scan_stall():
    import asyncio
    import time

    release = asyncio.Event()

    async def blocking_scan():
        await release.wait()
        time.sleep(0.05)

    scan_task = asyncio.create_task(blocking_scan())
    lag_task = asyncio.create_task(_measure_event_loop_lag(scan_task))
    await asyncio.sleep(0)
    release.set()

    await scan_task
    await asyncio.sleep(0)
    lag_ms = await lag_task

    assert lag_ms >= 30.0
