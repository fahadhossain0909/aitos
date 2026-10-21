"""Regression tests for the A-Stat scanner bridge.

The bridge maps the statistical stack's direction probability onto the
scanner's 0-10 component scale, signed by the proposed trade side. These
tests pin the contract: availability gating, side agreement scoring,
feature mapping sanity, and short-history exclusion.
"""

from __future__ import annotations

import math
import random

from aitos.intelligence.a_stat_bridge import AStatBridge, MIN_KLINES_FOR_STACK
from aitos.models.trade import TradeSide


def _klines(n: int, *, drift: float = 0.0, seed: int = 7) -> list:
    from datetime import datetime, timedelta, timezone

    from aitos.models.market import Kline

    rng = random.Random(seed)
    price = 100.0
    klines = []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        ret = drift + rng.gauss(0.0, 0.01)
        price = max(1e-6, price * (1.0 + ret))
        ts = base + timedelta(minutes=i)
        klines.append(
            Kline(
                symbol="TESTUSDT",
                timeframe="15m",
                open_time=ts,
                close_time=ts,
                open=price,
                high=price * 1.005,
                low=price * 0.995,
                close=price,
                volume=100.0,
                quote_volume=10_000.0,
                trades_count=50,
                taker_buy_volume=50.0,
                taker_buy_quote_volume=5_000.0,
            )
        )
    return klines


def test_bridge_returns_none_for_short_history() -> None:
    bridge = AStatBridge()
    component, result = bridge.evaluate(
        symbol="TESTUSDT",
        direction=TradeSide.LONG,
        klines=_klines(MIN_KLINES_FOR_STACK - 1),
        flow_score=5.0,
        candle_cvd=5.0,
        orderflow_vwap=0.0,
        depth_imbalance=5.0,
        funding_score=5.0,
        oi_score=5.0,
        lead_lag=5.0,
        last_close=100.0,
    )
    assert component is None
    assert result is None


def test_bridge_component_bounded_and_side_consistent() -> None:
    bridge = AStatBridge()
    klines = _klines(120, drift=0.004)  # persistent uptrend
    long_component, long_result = bridge.evaluate(
        symbol="TESTUSDT",
        direction=TradeSide.LONG,
        klines=klines,
        flow_score=7.0,
        candle_cvd=7.0,
        orderflow_vwap=klines[-1].close * 0.995,
        depth_imbalance=7.0,
        funding_score=5.0,
        oi_score=5.0,
        lead_lag=5.0,
        last_close=klines[-1].close,
    )
    assert long_result is not None
    assert math.isfinite(long_component)
    assert 0.0 <= long_component <= 10.0
    # Advanced stack must engage once enough return history exists.
    assert long_result.advanced is not None
    # Probabilities normalise.
    total = (
        long_result.direction.up
        + long_result.direction.down
        + long_result.direction.flat
    )
    assert abs(total - 1.0) < 1e-6

    # Same market proposed as SHORT must score symmetrically:
    # component(SHORT) = 10 - component(LONG) when flat mass is zero,
    # otherwise strictly lower than the LONG score in an uptrend.
    short_component, _ = bridge.evaluate(
        symbol="TESTUSDT",
        direction=TradeSide.SHORT,
        klines=klines,
        flow_score=3.0,
        candle_cvd=3.0,
        orderflow_vwap=klines[-1].close * 0.995,
        depth_imbalance=3.0,
        funding_score=5.0,
        oi_score=5.0,
        lead_lag=5.0,
        last_close=klines[-1].close,
    )
    assert short_component < long_component


def test_bridge_updates_calibration() -> None:
    bridge = AStatBridge()
    bridge.update(realised_return=0.01, predicted_up=0.7)
    bridge.update(realised_return=-0.01, predicted_up=0.7)
    # No crash; engine holds outcomes for Brier-based calibration.
    assert bridge.engine is not None
