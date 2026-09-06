import pytest

from aitos.kernel.ai_kernel import AIKernel, DecisionContext


class DummyBus:
    async def publish(self, event):
        return None


@pytest.mark.asyncio
async def test_kernel_includes_contextual_decision_in_fused_evidence():
    kernel = AIKernel(DummyBus(), require_human_approval_for_prod=False)
    await kernel.initialize({})
    result = await kernel.request_decision(
        DecisionContext(
            symbol="BTCUSDT",
            context={
                "direction": "long",
                "component_scores": {
                    "trend_strength": 8.0,
                    "liquidity_quality": 8.0,
                    "order_flow_bias": 8.0,
                    "auction_context": 7.0,
                    "volatility": 6.0,
                    "market_regime": 8.0,
                },
                "component_availability": {
                    "trend_strength": True,
                    "liquidity_quality": True,
                    "order_flow_bias": True,
                    "auction_context": True,
                    "volatility": True,
                    "market_regime": True,
                },
                "regime": "trending",
            },
        )
    )
    contextual = [
        x for x in result.contributions if x.get("source") == "contextual_decision"
    ]
    assert contextual
    assert contextual[0]["scenarios"]
    assert "invalidations" not in contextual[0]
