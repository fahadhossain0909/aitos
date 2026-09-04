from aitos.statistics import AStatEngine, AStatObservation, StatisticalStrategyRouter


def test_router_returns_all_strategy_families_without_execution_side_effects() -> None:
    result = AStatEngine().evaluate(
        AStatObservation(
            symbol="BTCUSDT",
            sample_size=250,
            features={"momentum": 0.8, "volatility": 0.03, "expected_return": 0.01},
        )
    )
    ranked = StatisticalStrategyRouter().rank(result)
    assert {item.strategy_id for item in ranked} == {"directional", "hedging", "options"}
    assert all(0.0 <= item.score <= 1.0 for item in ranked)
