from aitos.statistics import AStatEngine, AStatObservation, BayesianEvidence


def test_bayesian_evidence_increases_bullish_probability() -> None:
    engine = AStatEngine()
    prior = engine.bayesian_update(0.5, [BayesianEvidence("cvd", 3.0)])
    assert prior > 0.5


def test_result_is_strategy_agnostic_and_serialisable() -> None:
    engine = AStatEngine()
    result = engine.evaluate(
        AStatObservation(
            symbol="BTCUSDT",
            horizon="1h",
            sample_size=200,
            features={
                "momentum": 0.8,
                "orderbook_imbalance": 0.5,
                "cvd": 0.7,
                "volatility": 0.01,
                "average_win": 0.015,
                "average_loss": 0.008,
            },
        ),
        [BayesianEvidence("lead_lag", 1.4)],
    )
    context = result.for_strategy("directional")
    assert 0.0 <= result.direction.up <= 1.0
    assert (
        abs(result.direction.up + result.direction.down + result.direction.flat - 1.0)
        < 1e-9
    )
    assert 0.0 <= context.suitability <= 1.0
    payload = result.to_dict()
    assert payload["symbol"] == "BTCUSDT"


def test_online_calibration_updates_from_realised_outcomes() -> None:
    engine = AStatEngine(calibration_quality=0.5)
    baseline = engine._calibration()
    for _ in range(20):
        engine.update(0.01, 0.9)
    assert engine._calibration() != baseline
