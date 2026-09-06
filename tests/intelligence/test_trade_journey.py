from aitos.intelligence.trade_journey import (
    TradeJourneyAction,
    TradeJourneyEngine,
    TradeJourneyState,
)


def test_trade_journey_proves_then_extends():
    engine = TradeJourneyEngine(stale_after_seconds=900)
    proving = engine.evaluate(
        side="LONG",
        entry_price=100.0,
        current_price=100.2,
        unrealized_r=0.2,
        age_seconds=30,
        thesis_health=1.0,
        momentum=1.0,
        liquidity=0.9,
        structure=0.9,
        expected_path_prices=(101.0, 102.0),
    )
    assert proving.state == TradeJourneyState.PROVING
    assert proving.action == TradeJourneyAction.HOLD

    extending = engine.evaluate(
        side="LONG",
        entry_price=100.0,
        current_price=101.4,
        unrealized_r=1.4,
        age_seconds=120,
        thesis_health=1.0,
        momentum=1.0,
        liquidity=0.9,
        structure=0.9,
        expected_path_prices=(101.0, 102.0),
    )
    assert extending.state == TradeJourneyState.EXTENDING
    assert extending.action == TradeJourneyAction.TRAIL
    assert extending.path_adherence > 50.0


def test_trade_journey_detects_stale_trade():
    engine = TradeJourneyEngine(stale_after_seconds=300)
    snapshot = engine.evaluate(
        side="LONG",
        entry_price=100.0,
        current_price=100.03,
        unrealized_r=0.03,
        age_seconds=1200,
        thesis_health=0.55,
        momentum=0.38,
        liquidity=0.50,
        structure=0.60,
        expected_path_prices=(102.0, 104.0),
    )
    assert "stale_trade" in snapshot.reasons
    assert snapshot.time_efficiency < 50.0


def test_trade_journey_invalidates_on_broken_thesis_or_structure():
    engine = TradeJourneyEngine()
    snapshot = engine.evaluate(
        side="SHORT",
        entry_price=100.0,
        current_price=102.0,
        unrealized_r=-2.0,
        age_seconds=45,
        thesis_health=0.10,
        momentum=0.20,
        liquidity=0.30,
        structure=0.10,
        expected_path_prices=(99.0, 98.0),
    )
    assert snapshot.state == TradeJourneyState.EXITING
    assert snapshot.action == TradeJourneyAction.EXIT
    assert "thesis_or_structure_failure" in snapshot.reasons


def test_trade_journey_path_adherence_tracks_actual_progress():
    engine = TradeJourneyEngine()
    snapshot = engine.evaluate(
        side="LONG",
        entry_price=100.0,
        current_price=101.0,
        unrealized_r=1.0,
        age_seconds=60,
        thesis_health=1.0,
        momentum=0.8,
        liquidity=0.8,
        structure=0.9,
        expected_path_prices=(101.0, 102.0, 104.0),
    )
    assert snapshot.actual_progress > 0.0
    assert snapshot.path_adherence > 50.0
