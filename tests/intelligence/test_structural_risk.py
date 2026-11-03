"""Unit tests for Structural Risk Engine (Phase C)."""

from __future__ import annotations

from aitos.intelligence.structural_risk import StructuralRiskEngine, StructuralStop

# Hierarchy types returned by classify_swing / select_by_hierarchy
SWING_TYPES = (
    "protected_swing",
    "major_swing",
    "micro_swing",
    "structure_break",
    "value_area",
    "liquidity",
    "emergency_fallback",
)


def test_long_uses_swing_low():
    eng = StructuralRiskEngine()
    stop = eng.compute(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        swing_lows=[78600.0, 78200.0],
        atr=200.0,
    )
    assert isinstance(stop, StructuralStop)
    assert stop.side == "LONG"
    assert stop.stop_price < 79000.0
    assert stop.invalidation_type in SWING_TYPES
    assert stop.distance > 0


def test_short_uses_swing_high():
    eng = StructuralRiskEngine()
    stop = eng.compute(
        symbol="BTCUSDT",
        side="SHORT",
        entry_price=79000.0,
        swing_highs=[79400.0, 79800.0],
        atr=200.0,
    )
    assert stop.stop_price > 79000.0
    assert stop.invalidation_type in SWING_TYPES


def test_fallback_when_no_structure():
    eng = StructuralRiskEngine()
    stop = eng.compute(
        symbol="ETHUSDT",
        side="LONG",
        entry_price=3500.0,
    )
    assert stop.invalidation_type == "emergency_fallback"
    assert stop.stop_price < 3500.0
    assert 0 < stop.distance_pct <= 0.04


def test_max_stop_cap():
    eng = StructuralRiskEngine(config={"max_stop_pct": 0.02})
    stop = eng.compute(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        swing_lows=[70000.0],  # far away
    )
    assert stop.distance_pct <= 0.02 + 1e-6


def test_buffer_applied():
    eng = StructuralRiskEngine()
    stop = eng.compute(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=79000.0,
        swing_lows=[78600.0],
        atr=300.0,
    )
    assert stop.buffer_applied > 0
    # Stop should be below the raw swing low because of buffer
    assert stop.stop_price < 78600.0
