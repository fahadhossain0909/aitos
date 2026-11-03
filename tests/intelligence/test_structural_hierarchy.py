"""Structural hierarchy: prefer protected structure over micro swing."""

from aitos.intelligence.structural_risk import StructuralRiskEngine


def test_prefers_protected_swing_over_micro():
    engine = StructuralRiskEngine()
    stop = engine.compute(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        swing_lows=(99.8, 98.5),
        atr=1.0,
    )
    assert stop.stop_price < 99.5
    assert stop.invalidation_type != "micro_swing"


def test_structure_break_beats_swings():
    engine = StructuralRiskEngine()
    stop = engine.compute(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        swing_lows=(99.5, 98.0),
        structure_break_level=97.0,
        atr=0.5,
    )
    assert stop.invalidation_type == "structure_break"
    assert stop.stop_price < 97.5


def test_micro_only_when_no_better_candidate():
    engine = StructuralRiskEngine()
    stop = engine.compute(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        swing_lows=(99.85,),
        atr=0.3,
    )
    assert stop.invalidation_type == "micro_swing"
