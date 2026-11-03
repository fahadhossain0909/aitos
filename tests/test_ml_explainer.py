import random

from aitos.xai.ml_explainer import (
    DEFAULT_MIN_SAMPLES,
    FEATURE_ORDER,
    TradeOutcomeClassifier,
)


def make_scores(bias=0.0, seed=None):
    rng = random.Random(seed)
    return {
        f: max(0.0, min(10.0, 5.0 + bias + rng.uniform(-1.0, 1.0)))
        for f in FEATURE_ORDER
    }


def test_not_ready_before_min_samples():
    clf = TradeOutcomeClassifier(min_samples_for_ready=10)
    for i in range(5):
        clf.partial_fit(make_scores(seed=i), won=(i % 2 == 0))
    assert clf.is_ready is False
    assert clf.n_samples_seen == 5


def test_not_ready_if_only_one_class_seen():
    clf = TradeOutcomeClassifier(min_samples_for_ready=3)
    for i in range(5):
        clf.partial_fit(
            make_scores(seed=i), won=True
        )  # always "won" — SGDClassifier can't do proba with 1 class
    assert clf.is_ready is False


def test_ready_after_enough_samples_of_both_classes():
    clf = TradeOutcomeClassifier(min_samples_for_ready=10)
    for i in range(10):
        clf.partial_fit(make_scores(seed=i), won=(i % 2 == 0))
    assert clf.is_ready is True


def test_predict_win_probability_none_when_not_ready():
    clf = TradeOutcomeClassifier(min_samples_for_ready=10)
    clf.partial_fit(make_scores(seed=1), won=True)
    assert clf.predict_win_probability(make_scores(seed=2)) is None


def test_predict_win_probability_returns_value_between_0_and_1_when_ready():
    clf = TradeOutcomeClassifier(min_samples_for_ready=10)
    for i in range(10):
        clf.partial_fit(make_scores(seed=i), won=(i % 2 == 0))
    proba = clf.predict_win_probability(make_scores(seed=99))
    assert proba is not None
    assert 0.0 <= proba <= 1.0


def test_learns_a_real_pattern_winning_bias_predicts_higher_win_probability():
    """Train on a clear signal (high trend_strength correlates with winning)
    and confirm the model actually picked it up, not just clearing the
    ready threshold."""
    clf = TradeOutcomeClassifier(min_samples_for_ready=20)
    rng = random.Random(42)
    for i in range(80):
        won = rng.random() < 0.5
        scores = {f: 5.0 for f in FEATURE_ORDER}
        scores["trend_strength"] = 8.5 if won else 1.5
        # add a little noise to every other feature so it's not a trivial single-feature dataset
        for f in FEATURE_ORDER:
            if f != "trend_strength":
                scores[f] = max(0.0, min(10.0, scores[f] + rng.uniform(-0.5, 0.5)))
        clf.partial_fit(scores, won=won)

    assert clf.is_ready is True
    high_trend = {f: 5.0 for f in FEATURE_ORDER}
    high_trend["trend_strength"] = 9.0
    low_trend = {f: 5.0 for f in FEATURE_ORDER}
    low_trend["trend_strength"] = 1.0

    assert clf.predict_win_probability(high_trend) > clf.predict_win_probability(
        low_trend
    )


def test_explain_empty_when_not_ready():
    clf = TradeOutcomeClassifier(min_samples_for_ready=10)
    clf.partial_fit(make_scores(seed=1), won=True)
    assert clf.explain(make_scores(seed=2)) == {}


def test_explain_returns_shap_values_per_feature_when_ready():
    clf = TradeOutcomeClassifier(min_samples_for_ready=20)
    rng = random.Random(7)
    for i in range(30):
        won = rng.random() < 0.5
        scores = {f: 5.0 for f in FEATURE_ORDER}
        scores["order_flow_bias"] = 8.0 if won else 2.0
        clf.partial_fit(scores, won=won)

    shap_values = clf.explain(
        {f: 5.0 for f in FEATURE_ORDER} | {"order_flow_bias": 9.0}
    )
    assert set(shap_values.keys()) == set(FEATURE_ORDER)
    assert all(isinstance(v, float) for v in shap_values.values())
    # the feature that actually drives the label should have a non-trivial contribution
    assert abs(shap_values["order_flow_bias"]) > 0.01


def test_default_min_samples_is_reasonable():
    assert (
        DEFAULT_MIN_SAMPLES >= 10
    )  # sanity: shouldn't claim confidence from a tiny sample
