from aitos.intelligence.liquidity_intelligence import (
    Candle,
    LiquidityAction,
    LiquidityConfig,
    LiquidityLevel,
    LiquidityOutcome,
    LiquiditySide,
    LiquiditySource,
    OrderFlowEvidence,
    SweepEvent,
    SweepState,
    attribute_liquidity_outcome,
    classify_sweep,
    detect_liquidity_sweeps,
    map_liquidity_levels,
    plan_liquidity_aware_stop,
    position_quantity_for_risk,
    score_liquidity_risk,
)


def candles(rows):
    return [Candle(*row) for row in rows]


def test_equal_highs_and_lows_are_mapped_from_confirmed_pivots():
    rows = candles(
        [
            (100, 102, 99, 100, 10),
            (100, 104.95, 99, 103, 10),
            (103, 103, 98, 100, 10),
            (100, 104, 99, 102, 10),
            (102, 105.0, 98.5, 99, 10),
            (99, 103, 99, 101, 10),
            (101, 102, 98.01, 100, 10),
            (100, 101, 98.5, 99, 10),
        ]
    )
    levels = map_liquidity_levels("BTCUSDT", "5m", rows)
    assert any(
        x.side is LiquiditySide.BUY_SIDE and x.source is LiquiditySource.EQUAL_HIGHS
        for x in levels
    )
    assert any(
        x.side is LiquiditySide.SELL_SIDE and x.source is LiquiditySource.EQUAL_LOWS
        for x in levels
    )


def test_unclosed_candles_are_excluded():
    rows = candles(
        [
            (100, 102, 99, 101, 10),
            (101, 103, 100, 102, 10),
            (102, 104, 101, 103, 10),
            (103, 105, 102, 104, 10),
            (104, 106, 103, 105, 10),
            (105, 107, 104, 106, 10),
        ]
    )
    levels = map_liquidity_levels(
        "BTCUSDT", "1m", rows + [Candle(106, 120, 90, 110, 100, is_closed=False)]
    )
    assert all(x.price != 120 for x in levels)


def test_sell_side_sweep_and_rejection_require_fresh_bullish_flow():
    level = LiquidityLevel(
        "pool-1",
        "BTCUSDT",
        LiquiditySide.SELL_SIDE,
        100,
        LiquiditySource.EQUAL_LOWS,
        "5m",
        80,
        2,
    )
    rows = candles(
        [
            (101, 102, 100.5, 101, 10),
            (101, 101.5, 99.5, 100.5, 12),
            (100.5, 101, 98.8, 100.4, 30),
            (100.4, 102, 100.1, 101.5, 20),
            (101.5, 103, 101, 102.5, 18),
        ]
    )
    events = detect_liquidity_sweeps(
        [level], rows, config=LiquidityConfig(sweep_min_penetration_atr=0.01)
    )
    assert events
    event = events[-1]
    classification = classify_sweep(
        event,
        rows,
        flow=OrderFlowEvidence(cvd_score=7, ofi_score=7, fresh=True, confidence=0.9),
    )
    assert classification.action is LiquidityAction.REVERSAL_LONG


def test_sweep_without_flow_confirmation_waits_not_reverses():
    level = LiquidityLevel(
        "pool-2",
        "BTCUSDT",
        LiquiditySide.BUY_SIDE,
        100,
        LiquiditySource.EQUAL_HIGHS,
        "5m",
        80,
        2,
    )
    rows = candles(
        [
            (99, 100.5, 98.5, 99, 10),
            (99, 101.2, 98.8, 100.8, 20),
            (100.8, 101, 99.8, 100.2, 10),
        ]
    )
    event = detect_liquidity_sweeps(
        [level], rows, config=LiquidityConfig(sweep_min_penetration_atr=0.01)
    )[-1]
    result = classify_sweep(event, rows, flow=OrderFlowEvidence())
    assert result.action is LiquidityAction.WAIT


def test_buy_side_acceptance_is_continuation_not_short_reversal():
    level = LiquidityLevel(
        "pool-3",
        "BTCUSDT",
        LiquiditySide.BUY_SIDE,
        100,
        LiquiditySource.EQUAL_HIGHS,
        "5m",
        80,
        2,
    )
    rows = candles(
        [
            (99, 100.5, 98.5, 99, 10),
            (99, 101.2, 98.8, 100.8, 20),
            (100.8, 102, 100.5, 101.8, 20),
            (101.8, 103, 101.5, 102.8, 20),
        ]
    )
    event = detect_liquidity_sweeps(
        [level], rows, config=LiquidityConfig(sweep_min_penetration_atr=0.01)
    )[-1]
    result = classify_sweep(event, rows, flow=OrderFlowEvidence())
    assert result.action is LiquidityAction.CONTINUATION_LONG


def test_stop_buffer_and_position_sizing_preserve_risk_budget():
    plan = plan_liquidity_aware_stop(
        side="LONG", entry_price=100, invalidation_price=98, atr=1
    )
    assert plan.allowed
    assert plan.stop_price < 98
    qty = position_quantity_for_risk(
        equity=1000,
        risk_fraction=0.01,
        stop_distance=plan.distance,
        estimated_cost_per_unit=0.02,
    )
    assert qty * (plan.distance + 0.02) <= 10.0 + 1e-9


def test_stop_outside_max_distance_is_not_widened_without_risk_reduction():
    plan = plan_liquidity_aware_stop(
        side="SHORT", entry_price=100, invalidation_price=104, atr=1
    )
    assert not plan.allowed
    assert plan.reason == "stop_exceeds_max_distance_reduce_size_or_reject"


def test_risk_score_is_symmetric_for_long_and_short_and_bounded():
    low = LiquidityLevel(
        "low",
        "BTCUSDT",
        LiquiditySide.SELL_SIDE,
        99.5,
        LiquiditySource.EQUAL_LOWS,
        "5m",
        95,
        3,
    )
    high = LiquidityLevel(
        "high",
        "BTCUSDT",
        LiquiditySide.BUY_SIDE,
        100.5,
        LiquiditySource.EQUAL_HIGHS,
        "5m",
        95,
        3,
    )
    flow_long = OrderFlowEvidence(cvd_score=2, ofi_score=2, fresh=True, confidence=0.9)
    flow_short = OrderFlowEvidence(cvd_score=8, ofi_score=8, fresh=True, confidence=0.9)
    long_risk = score_liquidity_risk(
        100, [low], atr=1, direction="LONG", flow=flow_long
    )
    short_risk = score_liquidity_risk(
        100, [high], atr=1, direction="SHORT", flow=flow_short
    )
    assert long_risk.score == short_risk.score
    assert 0 <= long_risk.score <= 100


def test_outcome_attribution_uses_net_costs():
    row = attribute_liquidity_outcome(
        LiquidityOutcome(
            "event",
            "BTCUSDT",
            "LONG",
            "reversal",
            1.0,
            0.4,
            1.5,
            fees=0.1,
            funding=-0.02,
            slippage=0.03,
        )
    )
    assert row["net_pnl"] == 0.85
    assert row["profitable"]


def test_invalid_prices_fail_closed():
    import pytest

    with pytest.raises(ValueError):
        plan_liquidity_aware_stop(
            side="LONG", entry_price=100, invalidation_price=101, atr=1
        )


def test_liquidity_level_ids_are_stable_across_replays():
    rows = candles(
        [
            (100, 102, 99, 100, 10),
            (100, 104.95, 99, 103, 10),
            (103, 103, 98, 100, 10),
            (100, 104, 99, 102, 10),
            (102, 105.0, 98.5, 99, 10),
            (99, 103, 99, 101, 10),
            (101, 102, 98.01, 100, 10),
            (100, 101, 98.5, 99, 10),
        ]
    )
    first = map_liquidity_levels("BTCUSDT", "5m", rows)
    second = map_liquidity_levels("BTCUSDT", "5m", rows)
    assert [level.level_id for level in first] == [level.level_id for level in second]


def test_buy_side_rejection_with_bearish_flow_creates_short_candidate():
    level = LiquidityLevel(
        "pool-short",
        "BTCUSDT",
        LiquiditySide.BUY_SIDE,
        100,
        LiquiditySource.EQUAL_HIGHS,
        "5m",
        80,
        2,
    )
    rows = candles(
        [
            (99, 100.5, 98.5, 99, 10),
            (99, 101.2, 98.8, 100.8, 20),
            (100.8, 101, 99.8, 99.5, 15),
            (99.5, 100, 98.5, 98.8, 18),
        ]
    )
    event = detect_liquidity_sweeps(
        [level], rows, config=LiquidityConfig(sweep_min_penetration_atr=0.01)
    )[-1]
    result = classify_sweep(
        event,
        rows,
        flow=OrderFlowEvidence(
            cvd_score=3,
            ofi_score=3,
            fresh=True,
            confidence=0.9,
        ),
    )
    assert result.action is LiquidityAction.REVERSAL_SHORT


def test_sweep_event_id_is_stable_when_replay_window_moves():
    level = LiquidityLevel(
        "stable-pool",
        "BTCUSDT",
        LiquiditySide.SELL_SIDE,
        100,
        LiquiditySource.EQUAL_LOWS,
        "5m",
        80,
        2,
    )
    prefix = Candle(101, 102, 100.5, 101, 10, timestamp="prefix")
    rows = candles(
        [
            (101, 102, 100.5, 101, 10),
            (101, 101.5, 99.5, 100.5, 12),
            (100.5, 101, 98.8, 100.4, 30),
            (100.4, 102, 100.1, 101.5, 20),
        ]
    )
    rows[2] = Candle(100.5, 101, 98.8, 100.4, 30, timestamp="sweep-bar")
    config = LiquidityConfig(sweep_min_penetration_atr=0.01)
    first = detect_liquidity_sweeps([level], rows, config=config)
    second = detect_liquidity_sweeps([level], [prefix, *rows], config=config)
    assert first[-1].event_id == second[-1].event_id
