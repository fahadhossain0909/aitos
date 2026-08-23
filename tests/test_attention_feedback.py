import asyncio

import pytest

from aitos.core.contracts import Event
from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.attention_feedback import AttentionFeedbackLoop
from aitos.xai.ml_explainer import FEATURE_ORDER


async def _wait_for(predicate, timeout=3.0, interval=0.05):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


def make_agent_consensus():
    return {f: 6.0 for f in FEATURE_ORDER}


@pytest.mark.asyncio
async def test_feedback_loop_trains_explainer_from_closed_trade(event_bus):
    explainer = AttentionExplainer(min_samples_for_ready=1000)
    loop = AttentionFeedbackLoop(event_bus=event_bus, explainer=explainer)
    await loop.initialize({})

    payload = {
        "trade_id": "t1",
        "pnl": 100.0,
        "agent_consensus": make_agent_consensus(),
    }
    await event_bus.publish(
        Event(topic="trade.position_closed", payload=payload, source_module="test")
    )

    assert await _wait_for(lambda: loop.updates_applied == 1)
    assert explainer.n_samples_seen == 1

    await loop.shutdown()


@pytest.mark.asyncio
async def test_feedback_loop_skips_trades_without_agent_consensus(event_bus):
    explainer = AttentionExplainer()
    loop = AttentionFeedbackLoop(event_bus=event_bus, explainer=explainer)
    await loop.initialize({})

    payload = {"trade_id": "t1", "pnl": 100.0, "agent_consensus": {}}
    await event_bus.publish(
        Event(topic="trade.position_closed", payload=payload, source_module="test")
    )

    await asyncio.sleep(0.3)
    assert loop.updates_applied == 0

    await loop.shutdown()


@pytest.mark.asyncio
async def test_health_check_reports_state(event_bus):
    explainer = AttentionExplainer(min_samples_for_ready=2)
    loop = AttentionFeedbackLoop(event_bus=event_bus, explainer=explainer)
    await loop.initialize({})

    for i in range(2):
        payload = {
            "trade_id": f"t{i}",
            "pnl": 100.0 if i == 0 else -50.0,
            "agent_consensus": make_agent_consensus(),
        }
        await event_bus.publish(
            Event(topic="trade.position_closed", payload=payload, source_module="test")
        )

    assert await _wait_for(lambda: loop.updates_applied == 2)
    health = await loop.health_check()
    assert health.details["samples_seen"] == 2
    assert health.details["explainer_ready"] is True

    await loop.shutdown()
