from typing import Any

import pytest

from aitos.agents.base_agent import AgentDecision, BaseAgent
from aitos.core.exceptions import AgentNotRegisteredError, DecisionFusionError
from aitos.kernel.ai_kernel import Action, DecisionContext


class StubAgent(BaseAgent):
    """Minimal agent for tests: always votes a fixed direction/confidence."""

    def __init__(self, agent_id, event_bus, direction, confidence, weight=1.0):
        super().__init__(
            agent_id=agent_id, event_bus=event_bus, consensus_weight=weight
        )
        self._direction = direction
        self._confidence = confidence

    async def contribute_decision(self, context: dict[str, Any]) -> AgentDecision:
        return AgentDecision(
            agent_id=self.module_id,
            confidence=self._confidence,
            direction=self._direction,
            rationale=f"stub says {self._direction}",
        )


@pytest.mark.asyncio
async def test_register_and_deregister_agent(kernel, event_bus):
    agent = StubAgent("stub-1", event_bus, "long", 0.8)
    await agent.initialize({})

    await kernel.register_agent(agent)
    state = await kernel.get_world_state()
    assert "stub-1" in state.registered_agents

    await kernel.deregister_agent("stub-1")
    state = await kernel.get_world_state()
    assert "stub-1" not in state.registered_agents


@pytest.mark.asyncio
async def test_deregister_unknown_agent_raises(kernel):
    with pytest.raises(AgentNotRegisteredError):
        await kernel.deregister_agent("ghost-agent")


@pytest.mark.asyncio
async def test_request_decision_with_no_agents_raises(kernel):
    with pytest.raises(DecisionFusionError):
        await kernel.request_decision(DecisionContext(symbol="BTCUSDT"))


@pytest.mark.asyncio
async def test_request_decision_fuses_majority_direction(kernel, event_bus):
    bullish_a = StubAgent("bull-a", event_bus, "long", 0.9, weight=1.0)
    bullish_b = StubAgent("bull-b", event_bus, "long", 0.7, weight=1.0)
    bearish = StubAgent("bear-a", event_bus, "short", 0.6, weight=0.5)
    for a in (bullish_a, bullish_b, bearish):
        await a.initialize({})
        await kernel.register_agent(a)

    decision = await kernel.request_decision(DecisionContext(symbol="BTCUSDT"))

    assert decision.direction == "long"
    assert 0.0 < decision.confidence <= 1.0
    assert len(decision.contributions) == 3
    assert decision.conflicting_evidence  # bearish vote should surface as conflict


@pytest.mark.asyncio
async def test_governance_blocks_unapproved_production_action(kernel):
    action = Action(
        action_type="order.submit", payload={"symbol": "BTCUSDT"}, is_production=True
    )
    result = await kernel.enforce_governance(action)
    assert result.approved is False
    assert result.requires_human_approval is True


@pytest.mark.asyncio
async def test_governance_allows_approved_production_action(kernel):
    action = Action(
        action_type="order.submit",
        payload={"symbol": "BTCUSDT"},
        is_production=True,
        approved_by="fahad",
    )
    result = await kernel.enforce_governance(action)
    assert result.approved is True


@pytest.mark.asyncio
async def test_governance_allows_non_production_action_without_approval(kernel):
    action = Action(action_type="research.backtest", payload={}, is_production=False)
    result = await kernel.enforce_governance(action)
    assert result.approved is True
