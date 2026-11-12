"""Regression tests for the evidence-based AMD classifier."""
import pytest

from aitos.intelligence.amd_intelligence import (
    AMDConfig,
    AMDPhase,
    analyze_amd,
)
from aitos.intelligence.liquidity_intelligence import (
    Candle,
    LiquidityAction,
    LiquidityAnalysis,
    LiquidityLevel,
    LiquidityRisk,
    LiquiditySide,
    LiquiditySource,
    OrderFlowEvidence,
    SweepClassification,
    SweepEvent,
    SweepState,
)


def _liquidity(levels=(), sweeps=(), classification=None):
    return LiquidityAnalysis(
        symbol="BTCUSDT",
        timeframe="5m",
        levels=tuple(levels),
        sweeps=tuple(sweeps),
        latest_classification=classification,
        risk=LiquidityRisk(0.0, None, None, (), LiquidityAction.SHADOW),
    )


def _range_candles():
    rows = []
    for index in range(30):
        if index < 20:
            high, low, volume = 101.0 + (index % 2) * 0.2, 99.0 - (index % 2) * 0.2, 20.0
        else:
            high, low, volume = 100.2, 99.8, 10.0
        rows.append(
            Candle(
                open=100.0,
                high=high,
                low=low,
                close=100.0,
                volume=volume,
                timestamp=f"2026-11-12T00:{index:02d}:00Z",
            )
        )
    return rows


def test_accumulation_candidate_uses_completed_candles_and_internal_liquidity():
    level = LiquidityLevel(
        "range-pool",
        "BTCUSDT",
        LiquiditySide.BUY_SIDE,
        100.1,
        LiquiditySource.EQUAL_HIGHS,
        "5m",
        70.0,
        2,
    )
    rows = _range_candles()
    rows.append(
        Candle(100, 120, 80, 110, 1000, timestamp="forming", is_closed=False)
    )
    state = analyze_amd(
        "BTCUSDT",
        "5m",
        rows,
        liquidity=_liquidity(levels=(level,)),
        config=AMDConfig(window=30, min_window=20),
    )
    assert state.phase in {
        AMDPhase.ACCUMULATION_CANDIDATE,
        AMDPhase.ACCUMULATION_CONFIRMED,
    }
    assert state.range_high == 101.2
    assert state.range_low == 98.8
    assert state.as_of.endswith("29:00Z")
    assert "liquidity_pools_inside_range" in state.evidence


def test_distribution_confirmation_requires_structure_fresh_flow_and_expansion():
    level = LiquidityLevel(
        "sell-pool",
        "BTCUSDT",
        LiquiditySide.SELL_SIDE,
        99.0,
        LiquiditySource.EQUAL_LOWS,
        "5m",
        80.0,
        2,
    )
    rows = []
    for index in range(20):
        close = 100.0 + max(0, index - 14) * 0.25
        volume = 10.0 if index < 15 else 30.0
        rows.append(
            Candle(
                open=close - 0.05,
                high=close + 0.15,
                low=close - 0.15,
                close=close,
                volume=volume,
                timestamp=f"bar-{index}",
            )
        )
    sweep = SweepEvent(
        event_id="sweep-1",
        level=level,
        direction="down",
        sweep_price=98.5,
        penetration=0.5,
        candle_index=14,
        candle_timestamp="bar-14",
        wick_ratio=0.7,
        volume_zscore=2.0,
        state=SweepState.REJECTED,
    )
    classification = SweepClassification(
        SweepState.REJECTED,
        LiquidityAction.REVERSAL_LONG,
        0.9,
        ("reclaim", "fresh flow"),
        5,
    )
    state = analyze_amd(
        "BTCUSDT",
        "5m",
        rows,
        liquidity=_liquidity((level,), (sweep,), classification),
        flow=OrderFlowEvidence(
            cvd_score=8.0,
            ofi_score=7.0,
            fresh=True,
            confidence=0.9,
        ),
        structure_bias="bullish",
    )
    assert state.phase is AMDPhase.DISTRIBUTION_CONFIRMED
    assert state.direction == "LONG"
    assert state.liquidity_event_id == "sweep-1"


def test_stale_flow_cannot_confirm_distribution():
    level = LiquidityLevel(
        "sell-pool",
        "BTCUSDT",
        LiquiditySide.SELL_SIDE,
        99.0,
        LiquiditySource.EQUAL_LOWS,
        "5m",
        80.0,
        2,
    )
    rows = [
        Candle(
            100.0 + index * 0.01,
            100.2 + index * 0.01,
            99.8 + index * 0.01,
            100.1 + index * 0.01,
            10.0 if index < 15 else 30.0,
            timestamp=f"bar-{index}",
        )
        for index in range(20)
    ]
    sweep = SweepEvent(
        "sweep-2", level, "down", 98.5, 0.5, 14, "bar-14", 0.7, 2.0,
        state=SweepState.REJECTED,
    )
    classification = SweepClassification(
        SweepState.REJECTED,
        LiquidityAction.REVERSAL_LONG,
        0.9,
        ("reclaim",),
        5,
    )
    state = analyze_amd(
        "BTCUSDT",
        "5m",
        rows,
        liquidity=_liquidity((level,), (sweep,), classification),
        flow=OrderFlowEvidence(cvd_score=8.0, ofi_score=7.0, fresh=False),
        structure_bias="bullish",
    )
    assert state.phase is not AMDPhase.DISTRIBUTION_CONFIRMED


def test_amd_requires_at_least_one_completed_candle():
    with pytest.raises(ValueError):
        analyze_amd(
            "BTCUSDT",
            "1m",
            [Candle(100, 101, 99, 100, 10, is_closed=False)],
        )


def test_amd_state_id_is_deterministic_for_same_point_in_time_inputs():
    rows = _range_candles()
    first = analyze_amd("BTCUSDT", "5m", rows)
    second = analyze_amd("BTCUSDT", "5m", rows)
    assert first.state_id == second.state_id
    assert first.to_dict() == second.to_dict()


def test_amd_config_rejects_invalid_window_and_scores():
    with pytest.raises(ValueError):
        AMDConfig(window=10, min_window=20)
    with pytest.raises(ValueError):
        AMDConfig(min_distribution_score=1.5)
