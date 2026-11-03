import random

import pytest

from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.ml_explainer import FEATURE_ORDER


def make_context(**overrides):
    ctx = {f: 5.0 for f in FEATURE_ORDER}
    ctx.update(overrides)
    return ctx


def test_not_ready_before_min_samples():
    explainer = AttentionExplainer(min_samples_for_ready=10)
    for i in range(5):
        explainer.partial_fit(make_context(), won=(i % 2 == 0))
    assert explainer.is_ready is False


def test_not_ready_with_only_one_class():
    explainer = AttentionExplainer(min_samples_for_ready=3)
    for _ in range(5):
        explainer.partial_fit(make_context(), won=True)
    assert explainer.is_ready is False


def test_predict_and_attention_weights_none_or_empty_when_not_ready():
    explainer = AttentionExplainer(min_samples_for_ready=10)
    explainer.partial_fit(make_context(), won=True)
    assert explainer.predict_win_probability(make_context()) is None
    assert explainer.attention_weights(make_context()) == {}


def test_attention_weights_sum_to_one_and_are_nonnegative():
    explainer = AttentionExplainer(min_samples_for_ready=10)
    for i in range(10):
        explainer.partial_fit(make_context(), won=(i % 2 == 0))
    assert explainer.is_ready is True
    weights = explainer.attention_weights(make_context())
    assert set(weights.keys()) == set(FEATURE_ORDER)
    assert all(w >= 0.0 for w in weights.values())
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_single_example_loss_decreases_with_training_steps():
    """Basic correctness check: the numerical gradient should always be
    able to reduce loss on a single fixed example -- if this fails, the
    forward pass or gradient computation itself is broken."""
    import aitos.xai.attention_explainer as mod

    explainer = AttentionExplainer(learning_rate=0.5)
    x = mod._vectorize(make_context(trend_strength=9.0))
    losses = []
    for _ in range(15):
        losses.append(explainer._loss(x, 1.0, explainer._params))
        grads = explainer._numerical_gradient(x, 1.0)
        for key in explainer._param_keys:
            explainer._params[key] = explainer._params[key] - explainer._lr * grads[key]
    assert losses[-1] < losses[0]
    assert losses == sorted(losses, reverse=True)  # monotonically decreasing


@pytest.mark.asyncio
async def test_learns_to_discriminate_a_real_pattern():
    """The full behavioral test: train on trend_strength clearly
    predicting the outcome, confirm the model separates high/low inputs
    in its predicted probability -- not just that training runs without
    crashing."""
    explainer = AttentionExplainer(
        min_samples_for_ready=20, learning_rate=2.0, batch_size=8, random_state=1
    )
    rng = random.Random(101)

    for _ in range(70):
        trend = rng.uniform(0, 10)
        won = trend > 5.0
        explainer.partial_fit(make_context(trend_strength=trend), won=won)

    assert explainer.is_ready is True
    high_prob = explainer.predict_win_probability(make_context(trend_strength=9.0))
    low_prob = explainer.predict_win_probability(make_context(trend_strength=1.0))

    assert high_prob > low_prob
    assert high_prob > 0.7
    assert low_prob < 0.3


@pytest.mark.asyncio
async def test_attention_responds_to_the_informative_feature_value():
    """The actual "visualization" claim: once trained on a pattern driven
    by one feature, that feature's attention weight should swing
    substantially as its own value changes — proving the model is
    genuinely responsive to it, not just memorizing an average output.

    Note: attention weight direction doesn't necessarily track intuitive
    "importance" (well documented in attention-interpretability
    literature — see e.g. Jain & Wallace, 2019) — this model learned to
    attend heavily to order_flow_bias specifically when it signals a
    loss, and largely ignore it when it signals a win, which is a
    legitimate learned strategy, just not the naive "always look harder
    at the deciding feature" pattern. So the test checks *responsiveness*
    (variance across values), not a specific direction.
    """
    explainer = AttentionExplainer(
        min_samples_for_ready=20, learning_rate=2.0, batch_size=8, random_state=2
    )
    rng = random.Random(202)

    for _ in range(60):
        order_flow = rng.uniform(0, 10)
        won = order_flow > 5.0
        explainer.partial_fit(make_context(order_flow_bias=order_flow), won=won)

    informative_attn_by_value = [
        explainer.attention_weights(make_context(order_flow_bias=v))["order_flow_bias"]
        for v in (1.0, 5.0, 9.0)
    ]
    uninformative_attn_by_value = [
        explainer.attention_weights(make_context(order_flow_bias=v))["volatility"]
        for v in (1.0, 5.0, 9.0)
    ]

    informative_range = max(informative_attn_by_value) - min(informative_attn_by_value)
    uninformative_range = max(uninformative_attn_by_value) - min(
        uninformative_attn_by_value
    )

    assert informative_range > uninformative_range
    assert informative_range > 0.5  # a large, unmistakable swing, not noise
