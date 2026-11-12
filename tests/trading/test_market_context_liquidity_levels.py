from aitos.trading.market_context import CompositeContextProvider, MarketContext


class _StaticProvider:
    def __init__(self, context):
        self._context = context

    def get_context(self, symbol):
        return self._context


def test_market_context_forwards_and_merges_liquidity_levels():
    from aitos.intelligence.liquidity_intelligence import (
        LiquidityLevel,
        LiquiditySide,
        LiquiditySource,
    )

    pool = LiquidityLevel(
        level_id="ctx-pool",
        symbol="BTCUSDT",
        side=LiquiditySide.SELL_SIDE,
        price=78000.0,
        source=LiquiditySource.EQUAL_LOWS,
        timeframe="1h",
        strength=80.0,
        touch_count=2,
    )
    provider = CompositeContextProvider(
        _StaticProvider(
            MarketContext(
                liquidity_levels=(pool,),
                source="liquidity-test",
            )
        )
    )
    context = provider.get_context("BTCUSDT")
    assert context.liquidity_levels == (pool,)
    assert context.as_kwargs()["liquidity_levels"] == (pool,)
