from aitos.strategy import (
    CapitalAllocator,
    FundingBasisStrategy,
    MarketMakingStrategy,
    MarketSnapshot,
    RegimeRouterStrategy,
    StatisticalArbitrageStrategy,
    StrategyContext,
    StrategyEngine,
    StrategyMode,
    StrategyRegistry,
)


def test_regime_router_controls_strategy_families():
    registry = StrategyRegistry()
    registry.register(RegimeRouterStrategy())
    registry.register(MarketMakingStrategy())
    registry.register(FundingBasisStrategy())
    registry.register(StatisticalArbitrageStrategy())

    snap = MarketSnapshot(
        "BTC-PERP",
        100_000,
        spread_bps=8,
        liquidity_score=0.9,
        funding_rate=0.001,
        features={"spread_zscore": 3.0},
    )
    context = StrategyContext(
        now_ns=1,
        mode=StrategyMode.PAPER,
        snapshots={snap.instrument_id: snap},
        available_capital=10_000,
        risk_budget=5_000,
        global_regime="sideways",
    )
    cycle = StrategyEngine(registry, CapitalAllocator(10_000)).run_cycle(context)
    ids = {result.strategy_id for result in cycle.results}
    assert "market-making" in ids
    assert "funding-basis" in ids
    assert "statistical-arbitrage" not in ids
