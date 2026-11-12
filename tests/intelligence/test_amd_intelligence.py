"""Regression tests for the evidence-based AMD classifier."""

import json
from dataclasses import replace

import pytest

from aitos.intelligence.amd_intelligence import (
    AMDConfig,
    AMDPhase,
    AMDState,
    analyze_amd,
    analyze_amd_timeframe_stack,
    load_amd_checkpoint,
    save_amd_checkpoint,
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
            high = 101.0 + (index % 2) * 0.2
            low = 99.0 - (index % 2) * 0.2
            volume = 20.0
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
    rows.append(Candle(100, 120, 80, 110, 1000, timestamp="forming", is_closed=False))
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
        "sweep-2",
        level,
        "down",
        98.5,
        0.5,
        14,
        "bar-14",
        0.7,
        2.0,
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


def test_timeframe_stack_exposes_missing_frames_instead_of_fabricating_consensus():
    state = AMDState(
        symbol="BTCUSDT",
        timeframe="5m",
        phase=AMDPhase.DISTRIBUTION_CONFIRMED,
        direction="LONG",
        state_id="state-5m",
        as_of="bar-1",
        accumulation_score=0.0,
        manipulation_score=0.8,
        distribution_score=1.0,
        confidence=0.9,
        evidence_coverage=1.0,
        range_high=101.0,
        range_low=99.0,
        liquidity_event_id="sweep-1",
        target_liquidity_ids=(),
        evidence=("fresh_flow",),
    )
    stack = analyze_amd_timeframe_stack(
        {"5m": state},
        required_timeframes=("1h", "15m", "5m"),
    )
    assert stack.direction == "LONG"
    assert stack.available_timeframes == ("5m",)
    assert stack.missing_timeframes == ("1h", "15m")
    assert not stack.stack_complete
    assert stack.evidence_coverage < 1.0


def test_timeframe_stack_rejects_mixed_symbols():
    first = AMDState(
        "BTCUSDT",
        "5m",
        AMDPhase.NEUTRAL,
        "",
        "a",
        "t",
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        None,
        None,
        None,
        (),
        (),
    )
    second = AMDState(
        "ETHUSDT",
        "1h",
        AMDPhase.NEUTRAL,
        "",
        "b",
        "t",
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        None,
        None,
        None,
        (),
        (),
    )
    with pytest.raises(ValueError):
        analyze_amd_timeframe_stack({"5m": first, "1h": second})


def test_bearish_distribution_confirmation_is_symmetric():
    level = LiquidityLevel(
        "buy-pool",
        "BTCUSDT",
        LiquiditySide.BUY_SIDE,
        101.0,
        LiquiditySource.EQUAL_HIGHS,
        "5m",
        80.0,
        2,
    )
    rows = []
    for index in range(20):
        close = 100.0 - max(0, index - 14) * 0.25
        volume = 10.0 if index < 15 else 30.0
        rows.append(
            Candle(
                open=close + 0.05,
                high=close + 0.15,
                low=close - 0.15,
                close=close,
                volume=volume,
                timestamp=f"bear-bar-{index}",
            )
        )
    sweep = SweepEvent(
        "buy-sweep",
        level,
        "up",
        101.5,
        0.5,
        14,
        "bear-bar-14",
        0.7,
        2.0,
        state=SweepState.REJECTED,
    )
    classification = SweepClassification(
        SweepState.REJECTED,
        LiquidityAction.REVERSAL_SHORT,
        0.9,
        ("rejection", "fresh flow"),
        5,
    )
    state = analyze_amd(
        "BTCUSDT",
        "5m",
        rows,
        liquidity=_liquidity((level,), (sweep,), classification),
        flow=OrderFlowEvidence(
            cvd_score=2.0,
            ofi_score=3.0,
            fresh=True,
            confidence=0.9,
        ),
        structure_bias="bearish",
    )
    assert state.phase is AMDPhase.DISTRIBUTION_CONFIRMED
    assert state.direction == "SHORT"


class _CheckpointRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, key_count, key, as_of, payload, ttl):
        current = self.values.get(key)
        if current and json.loads(current)["as_of"] > as_of:
            return 0
        self.values[key] = payload
        return 1


@pytest.mark.asyncio
async def test_amd_checkpoint_round_trip_and_stale_write_rejection():
    redis = _CheckpointRedis()
    state = analyze_amd("BTCUSDT", "5m", _range_candles())
    assert await save_amd_checkpoint(redis, state)
    restored = await load_amd_checkpoint(redis, "BTCUSDT", "5m")
    assert restored == state

    older = replace(state, as_of="2026-11-11T00:00:00+00:00")
    assert not await save_amd_checkpoint(redis, older)
    assert await load_amd_checkpoint(redis, "BTCUSDT", "5m") == state


@pytest.mark.asyncio
async def test_amd_checkpoint_is_optional_without_redis():
    state = analyze_amd("BTCUSDT", "5m", _range_candles())
    assert await load_amd_checkpoint(None, "BTCUSDT", "5m") is None
    assert not await save_amd_checkpoint(None, state)


def test_previous_direction_turns_confirmed_balance_into_reaccumulation():
    previous = AMDState(
        symbol="BTCUSDT",
        timeframe="5m",
        phase=AMDPhase.DISTRIBUTION_CONFIRMED,
        direction="LONG",
        state_id="prior-trend",
        as_of="2026-11-11T23:55:00Z",
        accumulation_score=0.0,
        manipulation_score=0.8,
        distribution_score=1.0,
        confidence=1.0,
        evidence_coverage=1.0,
        range_high=102.0,
        range_low=98.0,
        liquidity_event_id="prior-sweep",
        target_liquidity_ids=(),
        evidence=("prior_delivery",),
    )
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
    state = analyze_amd(
        "BTCUSDT",
        "5m",
        _range_candles(),
        liquidity=_liquidity(levels=(level,)),
        previous_state=previous,
    )
    assert state.phase is AMDPhase.REACCUMULATION
    assert state.direction == "LONG"


def test_future_checkpoint_is_ignored_for_historical_replay():
    future = AMDState(
        symbol="BTCUSDT",
        timeframe="5m",
        phase=AMDPhase.DISTRIBUTION_CONFIRMED,
        direction="LONG",
        state_id="future-state",
        as_of="2026-11-13T00:00:00Z",
        accumulation_score=0.0,
        manipulation_score=0.8,
        distribution_score=1.0,
        confidence=1.0,
        evidence_coverage=1.0,
        range_high=102.0,
        range_low=98.0,
        liquidity_event_id="future-sweep",
        target_liquidity_ids=(),
        evidence=("future",),
    )
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
    state = analyze_amd(
        "BTCUSDT",
        "5m",
        _range_candles(),
        liquidity=_liquidity(levels=(level,)),
        previous_state=future,
    )
    assert state.phase is AMDPhase.ACCUMULATION_CONFIRMED
    assert state.direction == ""
