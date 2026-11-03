from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
from aitos.xai.ml_explainer import TradeOutcomeClassifier


def test_deep_value_scorer_persists_samples(tmp_path):
    path = tmp_path / "deep_value.pkl"
    model = DeepValueRLScorer(state_path=str(path))
    context = {"trend_strength": 8.0, "liquidity_quality": 7.0, "order_flow_bias": 6.0}
    model.update("BTCUSDT", context, 0.5)
    model.save_state()

    restored = DeepValueRLScorer(state_path=str(path))
    assert restored.load_state()
    assert restored.n_samples_seen == 1
    assert restored.is_fitted


def test_trade_outcome_classifier_persists_samples(tmp_path):
    path = tmp_path / "classifier.pkl"
    model = TradeOutcomeClassifier(min_samples_for_ready=1, state_path=str(path))
    scores = {"trend_strength": 8.0, "liquidity_quality": 7.0}
    model.partial_fit(scores, won=True)
    model.partial_fit(scores, won=False)
    model.save_state()

    restored = TradeOutcomeClassifier(min_samples_for_ready=1, state_path=str(path))
    assert restored.load_state()
    assert restored.n_samples_seen == 2
    assert restored.is_ready
