from aitos.learning.worker import ContinualLearningWorker


def test_numeric_features_filters_non_numeric_values():
    result = ContinualLearningWorker._numeric_features(
        '{"trend_strength": 7, "volatility": 3.5, "direction": "LONG", "ok": true}'
    )
    assert result == {"trend_strength": 7.0, "volatility": 3.5, "ok": 1.0}


def test_numeric_features_handles_invalid_json():
    assert ContinualLearningWorker._numeric_features("not-json") == {}
