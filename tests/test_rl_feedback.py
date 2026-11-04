import asyncio

import pytest

from aitos.core.contracts import Event
from aitos.intelligence.rl_feedback import RLFeedbackLoop
from aitos.intelligence.rl_policy import TabularBanditRLScorer


@pytest.mark.asyncio
async def test_cold_start_returns_neutral_score():
    scorer = TabularBanditRLScorer()
    score = await scorer.score("BTCUSDT", {"regime": "trending", "direction": "LONG"})
    assert score == 5.0


def test_sample_count_tracks_updates():
    scorer = TabularBanditRLScorer()
    assert scorer.sample_count("BTCUSDT", "trending", "LONG") == 0
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=1.0
    )
    assert scorer.sample_count("BTCUSDT", "trending", "LONG") == 1


@pytest.mark.asyncio
async def test_positive_rewards_push_score_above_neutral():
    scorer = TabularBanditRLScorer(min_samples_for_confidence=1)
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=1.5
    )
    score = await scorer.score("BTCUSDT", {"regime": "trending", "direction": "LONG"})
    assert score > 5.0


@pytest.mark.asyncio
async def test_negative_rewards_push_score_below_neutral():
    scorer = TabularBanditRLScorer(min_samples_for_confidence=1)
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=-1.5
    )
    score = await scorer.score("BTCUSDT", {"regime": "trending", "direction": "LONG"})
    assert score < 5.0


@pytest.mark.asyncio
async def test_low_confidence_bucket_is_shrunk_toward_neutral():
    scorer = TabularBanditRLScorer(min_samples_for_confidence=10)
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=2.0
    )  # only 1 of 10 needed samples
    score = await scorer.score("BTCUSDT", {"regime": "trending", "direction": "LONG"})
    assert (
        5.0 < score < 7.0
    )  # pulled well back from what a confident +2R average would give


@pytest.mark.asyncio
async def test_score_clamped_to_valid_range_for_extreme_rewards():
    scorer = TabularBanditRLScorer(
        min_samples_for_confidence=1, reward_scale_r_multiples=1.0
    )
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=100.0
    )
    score = await scorer.score("BTCUSDT", {"regime": "trending", "direction": "LONG"})
    assert score == 10.0


@pytest.mark.asyncio
async def test_buckets_are_independent_per_symbol_regime_direction():
    scorer = TabularBanditRLScorer(min_samples_for_confidence=1)
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=2.0
    )
    btc_score = await scorer.score(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}
    )
    eth_score = await scorer.score(
        "ETHUSDT", {"regime": "trending", "direction": "LONG"}
    )
    assert btc_score != eth_score
    assert eth_score == 5.0  # untouched bucket stays neutral


@pytest.mark.asyncio
async def test_running_mean_updates_incrementally():
    scorer = TabularBanditRLScorer(min_samples_for_confidence=1)
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=1.0
    )
    scorer.update(
        "BTCUSDT", {"regime": "trending", "direction": "LONG"}, reward_r_multiple=3.0
    )
    assert scorer._means[("BTCUSDT", "trending", "LONG")] == pytest.approx(2.0)
    assert scorer.sample_count("BTCUSDT", "trending", "LONG") == 2


async def _wait_for(predicate, timeout=3.0, interval=0.05):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


@pytest.mark.asyncio
async def test_rl_feedback_loop_trains_scorer_from_closed_trade_event(event_bus):
    scorer = TabularBanditRLScorer(min_samples_for_confidence=1)
    loop = RLFeedbackLoop(event_bus=event_bus, scorer=scorer)
    await loop.initialize({})

    trade_payload = {
        "trade_id": "t1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "regime": "trending",
        "pnl": 200.0,
        "risk_amount_usd": 100.0,
    }
    await event_bus.publish(
        Event(
            topic="trade.position_closed", payload=trade_payload, source_module="test"
        )
    )

    assert await _wait_for(lambda: loop.updates_applied == 1)
    assert scorer.sample_count("BTCUSDT", "trending", "LONG") == 1
    assert scorer._means[("BTCUSDT", "trending", "LONG")] == pytest.approx(
        2.0
    )  # 200/100 = 2R

    await loop.shutdown()


@pytest.mark.asyncio
async def test_rl_feedback_loop_skips_trades_missing_risk_amount(event_bus):
    scorer = TabularBanditRLScorer()
    loop = RLFeedbackLoop(event_bus=event_bus, scorer=scorer)
    await loop.initialize({})

    trade_payload = {
        "trade_id": "t1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "regime": "trending",
        "pnl": 50.0,
        "risk_amount_usd": 0.0,
    }
    await event_bus.publish(
        Event(
            topic="trade.position_closed", payload=trade_payload, source_module="test"
        )
    )

    await asyncio.sleep(0.3)
    assert loop.updates_applied == 0

    await loop.shutdown()


@pytest.mark.asyncio
async def test_rl_feedback_loop_health_check(event_bus):
    scorer = TabularBanditRLScorer(min_samples_for_confidence=1)
    loop = RLFeedbackLoop(event_bus=event_bus, scorer=scorer)
    await loop.initialize({})

    await event_bus.publish(
        Event(
            topic="trade.position_closed",
            payload={
                "trade_id": "t1",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "regime": "trending",
                "pnl": 100.0,
                "risk_amount_usd": 100.0,
            },
            source_module="test",
        )
    )
    await _wait_for(lambda: loop.updates_applied == 1)

    health = await loop.health_check()
    assert health.details["updates_applied"] == 1

    await loop.shutdown()

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("risk_amount", "pnl"),
    [(-1.0, 10.0), (float("nan"), 10.0), (10.0, float("inf"))],
)
async def test_rl_feedback_rejects_non_finite_or_negative_values(
    event_bus, risk_amount, pnl
):
    scorer = TabularBanditRLScorer()
    loop = RLFeedbackLoop(event_bus=event_bus, scorer=scorer)
    await loop.initialize({})
    await event_bus.publish(
        Event(
            topic="trade.position_closed",
            payload={
                "trade_id": "invalid",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "regime": "trending",
                "pnl": pnl,
                "risk_amount_usd": risk_amount,
            },
            source_module="test",
        )
    )
    await asyncio.sleep(0.2)
    assert loop.updates_applied == 0
    await loop.shutdown()


@pytest.mark.asyncio
async def test_rl_feedback_deduplicates_redelivered_closed_trade_for_deep_scorer():
    from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        scorer = DeepValueRLScorer(state_path=str(Path(directory) / "model.pkl"))
        loop = RLFeedbackLoop(event_bus=None, scorer=scorer)
        event = type(
            "E",
            (),
            {
                "payload": {
                    "trade_id": "trade-unique",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "regime": "trending",
                    "pnl": 20.0,
                    "risk_amount_usd": 10.0,
                },
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        )()
        await loop._on_position_closed(event)
        await loop._on_position_closed(event)
        assert scorer.n_samples_seen == 1
        assert loop.updates_applied == 1
