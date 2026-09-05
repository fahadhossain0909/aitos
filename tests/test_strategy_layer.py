from aitos.strategy import (
    CapitalAllocator,
    CapitalRequest,
    FundingBasisStrategy,
    MarketMakingStrategy,
    MarketSnapshot,
    RegimeRouterStrategy,
    StatisticalArbitrageStrategy,
    StrategyContext,
    StrategyFamily,
    StrategyMode,
    StrategyRegistry,
)


def context(**kwargs):
    return StrategyContext(
        now_ns=1,
        mode=StrategyMode.PAPER,
        available_capital=10_000,
        risk_budget=5_000,
        **kwargs,
    )


def test_registry_dispatches_enabled_strategies():
    registry = StrategyRegistry()
    registry.register(RegimeRouterStrategy())
    registry.register(FundingBasisStrategy())
    results = registry.evaluate(context(global_regime="sideways"))
    assert {r.strategy_id for r in results} == {"funding-basis", "regime-router"}


def test_funding_strategy_requires_net_edge():
    strategy = FundingBasisStrategy(min_funding_rate=0.001, min_edge_bps=2)
    snap = MarketSnapshot("BTC-PERP", 100_000, funding_rate=0.0015, basis_bps=3)
    result = strategy.evaluate(context(snapshots={snap.instrument_id: snap}))
    assert result.family is StrategyFamily.FUNDING_BASIS
    assert result.intents
    assert result.capital_request is not None


def test_stat_arb_uses_spread_zscore():
    snap = MarketSnapshot("BTC-PERP", 100_000, features={"spread_zscore": 2.5})
    result = StatisticalArbitrageStrategy().evaluate(
        context(snapshots={snap.instrument_id: snap})
    )
    assert result.intents[0].side == "sell"


def test_market_maker_respects_inventory_cap():
    snap = MarketSnapshot("BTC", 100_000, spread_bps=8, liquidity_score=0.9)
    result = MarketMakingStrategy().evaluate(
        context(snapshots={snap.instrument_id: snap}, positions={"BTC": 0})
    )
    assert len(result.intents) == 2
    assert all(i.order_type == "limit" for i in result.intents)


def test_allocator_never_exceeds_budget_or_per_strategy_cap():
    allocator = CapitalAllocator(10_000, max_strategy_fraction=0.5)
    requests = [
        CapitalRequest("a", 9_000, 100, 0.1, priority=2),
        CapitalRequest("b", 9_000, 100, 0.2, priority=1),
    ]
    allocations = allocator.allocate(requests)
    assert sum(a.notional for a in allocations) <= 10_000
    assert all(a.notional <= 5_000 for a in allocations)
