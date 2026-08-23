import random

import pytest

from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.xai.ml_explainer import FEATURE_ORDER


def make_context(**overrides):
    ctx = {f: 5.0 for f in FEATURE_ORDER}
    ctx.update(overrides)
    return ctx


@pytest.mark.asyncio
async def test_cold_start_returns_neutral_score():
    scorer = DeepValueRLScorer()
    score = await scorer.score("BTCUSDT", make_context())
    assert score == 5.0


def test_not_fitted_before_any_update():
    scorer = DeepValueRLScorer()
    assert scorer.is_fitted is False
    assert scorer.n_samples_seen == 0


def test_fitted_after_one_update():
    scorer = DeepValueRLScorer()
    scorer.update("BTCUSDT", make_context(), reward_r_multiple=1.0)
    assert scorer.is_fitted is True
    assert scorer.n_samples_seen == 1


@pytest.mark.asyncio
async def test_low_sample_count_shrinks_score_toward_neutral():
    scorer = DeepValueRLScorer(min_samples_for_confidence=100)
    scorer.update("BTCUSDT", make_context(trend_strength=9.0), reward_r_multiple=2.0)
    score = await scorer.score("BTCUSDT", make_context(trend_strength=9.0))
    assert 5.0 < score < 6.0  # heavily shrunk — only 1 of 100 needed samples


@pytest.mark.asyncio
async def test_learns_a_real_pattern_and_generalizes_to_unseen_but_similar_context():
    """The actual value-add over the tabular bandit: predictions for a
    feature vector never seen verbatim during training, but similar to
    ones that were, should still reflect the learned pattern — a lookup
    table couldn't do this at all."""
    scorer = DeepValueRLScorer(min_samples_for_confidence=20, learning_rate_init=0.05)
    rng = random.Random(11)

    for _ in range(300):
        trend = rng.uniform(0, 10)
        reward = (
            2.0 if trend > 7.0 else (-2.0 if trend < 3.0 else rng.uniform(-0.5, 0.5))
        )
        context = make_context(trend_strength=trend)
        scorer.update("BTCUSDT", context, reward_r_multiple=reward)

    assert scorer.n_samples_seen == 300

    # 8.3 was never used as an exact training value (rng draws are continuous),
    # but it's in the "trend > 7 -> should score well" region.
    high_trend_score = await scorer.score("BTCUSDT", make_context(trend_strength=8.3))
    low_trend_score = await scorer.score("BTCUSDT", make_context(trend_strength=1.7))

    assert high_trend_score > low_trend_score
    assert high_trend_score > 5.5
    assert low_trend_score < 4.5


@pytest.mark.asyncio
async def test_score_always_within_valid_range():
    scorer = DeepValueRLScorer(min_samples_for_confidence=1)
    for _ in range(20):
        scorer.update(
            "BTCUSDT", make_context(), reward_r_multiple=random.uniform(-50, 50)
        )
    score = await scorer.score("BTCUSDT", make_context())
    assert 0.0 <= score <= 10.0
