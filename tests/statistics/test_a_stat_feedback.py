import asyncio

import pytest

from aitos.core.contracts import Event
from aitos.statistics.engine import AStatEngine
from aitos.statistics.feedback import AStatFeedbackLoop


async def _wait_for(predicate, timeout=3.0):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.05)
        elapsed += 0.05
    return False


def _closed_trade(trade_id="trade-1", pnl_percent=1.25):
    return {
        "trade_id": trade_id,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "pnl_percent": pnl_percent,
        "exit_time": "2026-11-08T12:00:00+00:00",
        "agent_consensus": {
            "candidate_id": "candidate-1",
            "statistical_forecast": {"predicted_up": 0.72},
        },
    }


@pytest.mark.asyncio
async def test_a_stat_feedback_updates_calibration_from_closed_trade(event_bus, tmp_path):
    engine = AStatEngine(calibration_window=16)
    loop = AStatFeedbackLoop(
        event_bus, engine, tmp_path / "a-stat-feedback.sqlite3", calibration_window=16
    )
    await loop.initialize({})
    await event_bus.publish(
        Event(
            topic="trade.position_closed",
            payload=_closed_trade(),
            source_module="test",
        )
    )

    assert await _wait_for(lambda: loop._updates_applied == 1)
    assert len(engine._outcomes) == 1
    assert engine._outcomes[-1] == (0.72, True)
    health = await loop.health_check()
    assert health.details["durable_outcomes"] == 1
    await loop.shutdown()


@pytest.mark.asyncio
async def test_a_stat_feedback_is_idempotent_and_restores_calibration(
    event_bus, tmp_path
):
    database = tmp_path / "a-stat-feedback.sqlite3"
    first_engine = AStatEngine(calibration_window=16)
    first = AStatFeedbackLoop(event_bus, first_engine, database, calibration_window=16)
    await first.initialize({})
    event = Event(
        topic="trade.position_closed",
        payload=_closed_trade("trade-restore", -0.5),
        source_module="test",
    )
    await event_bus.publish(event)
    assert await _wait_for(lambda: first._updates_applied == 1)
    await first.shutdown()

    restored_engine = AStatEngine(calibration_window=16)
    restored = AStatFeedbackLoop(
        event_bus, restored_engine, database, calibration_window=16
    )
    await restored.initialize({})
    assert restored._updates_restored == 1
    assert len(restored_engine._outcomes) == 1
    assert restored_engine._outcomes[-1] == (0.72, False)

    await event_bus.publish(event)
    await asyncio.sleep(0.2)
    assert restored._updates_applied == 0
    assert len(restored_engine._outcomes) == 1
    await restored.shutdown()


@pytest.mark.asyncio
async def test_a_stat_feedback_ignores_closed_trades_without_statistical_forecast(
    event_bus, tmp_path
):
    engine = AStatEngine()
    loop = AStatFeedbackLoop(event_bus, engine, tmp_path / "a-stat-feedback.sqlite3")
    await loop.initialize({})
    await event_bus.publish(
        Event(
            topic="trade.position_closed",
            payload={
                "trade_id": "trade-no-forecast",
                "symbol": "ETHUSDT",
                "pnl_percent": 2.0,
                "agent_consensus": {"candidate_id": "candidate-no-forecast"},
            },
            source_module="test",
        )
    )
    await asyncio.sleep(0.2)
    assert loop._updates_applied == 0
    assert len(engine._outcomes) == 0
    await loop.shutdown()


@pytest.mark.asyncio
async def test_a_stat_feedback_rejects_invalid_probability_without_learning(
    event_bus, tmp_path
):
    engine = AStatEngine()
    loop = AStatFeedbackLoop(event_bus, engine, tmp_path / "a-stat-feedback.sqlite3")
    await loop.initialize({})
    payload = _closed_trade("trade-invalid")
    payload["agent_consensus"]["statistical_forecast"]["predicted_up"] = 1.5
    await event_bus.publish(
        Event(topic="trade.position_closed", payload=payload, source_module="test")
    )
    await asyncio.sleep(0.2)
    assert loop._updates_applied == 0
    assert len(engine._outcomes) == 0
    await loop.shutdown()
