from aitos.learning.worker import ContinualLearningWorker


def test_numeric_features_filters_non_numeric_values():
    result = ContinualLearningWorker._numeric_features(
        '{"trend_strength": 7, "volatility": 3.5, "direction": "LONG", "ok": true}'
    )
    assert result == {
        "trend_strength": 7.0,
        "volatility": 3.5,
        "ok": 1.0,
        "rl_confidence": 5.0,
    }


def test_numeric_features_handles_invalid_json():
    assert ContinualLearningWorker._numeric_features("not-json") == {}


def test_numeric_features_excludes_outcome_leakage_and_sets_neutral_rl_confidence():
    result = ContinualLearningWorker._numeric_features(
        '{"trend_strength": 7, "risk_amount_usd": 100, "pnl": 20, "reward": 2}'
    )
    assert result == {"trend_strength": 7.0, "rl_confidence": 5.0}


def test_backtest_reward_is_normalized_to_r_multiple():
    assert (
        ContinualLearningWorker._reward_r_multiple(
            {
                "decision": "backtest_trade",
                "outcome": "closed",
                "reward": 50.0,
                "features_json": "{}",
                "risk_state_json": '{"risk_amount_usd": 25.0}',
                "metadata_json": '{"reward_unit": "quote_pnl"}',
            }
        )
        == 2.0
    )


def test_backtest_r_multiple_reward_is_not_normalized_twice():
    assert (
        ContinualLearningWorker._reward_r_multiple(
            {
                "decision": "backtest_trade",
                "outcome": "closed",
                "reward": -0.75,
                "features_json": "{}",
                "risk_state_json": "{}",
                "metadata_json": '{"reward_unit": "r_multiple"}',
            }
        )
        == -0.75
    )


def test_backtest_summary_and_unscaled_trade_rewards_are_rejected():
    summary = {
        "decision": "backtest_summary",
        "outcome": "completed",
        "reward": 0.25,
        "metadata_json": "{}",
    }
    unscaled = {
        "decision": "backtest_trade",
        "outcome": "closed",
        "reward": 50.0,
        "features_json": "{}",
        "risk_state_json": "{}",
        "metadata_json": '{"reward_unit": "quote_pnl"}',
    }
    assert ContinualLearningWorker._reward_r_multiple(summary) is None
    assert ContinualLearningWorker._reward_r_multiple(unscaled) is None


def test_backtest_reward_rejects_invalid_risk_and_non_finite_values():
    invalid_risk = {
        "decision": "backtest_trade",
        "outcome": "closed",
        "reward": 1.0,
        "features_json": "{}",
        "risk_state_json": '{"risk_amount_usd": 0}',
        "metadata_json": "{}",
    }
    invalid_reward = {
        "decision": "backtest_trade",
        "outcome": "closed",
        "reward": float("nan"),
        "features_json": "{}",
        "risk_state_json": '{"risk_amount_usd": 10}',
        "metadata_json": "{}",
    }
    assert ContinualLearningWorker._reward_r_multiple(invalid_risk) is None
    assert ContinualLearningWorker._reward_r_multiple(invalid_reward) is None


def test_worker_trains_only_valid_r_normalized_backtest_trades():
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker._processed = set()
    worker._rows = lambda: [
        {
            "experience_id": "summary-1",
            "symbol": "BTCUSDT",
            "decision": "backtest_summary",
            "outcome": "completed",
            "reward": 0.25,
            "features_json": '{"trend_strength": 8}',
            "risk_state_json": "{}",
            "metadata_json": "{}",
        },
        {
            "experience_id": "unscaled-1",
            "symbol": "BTCUSDT",
            "decision": "backtest_trade",
            "outcome": "closed",
            "reward": 50.0,
            "features_json": '{"trend_strength": 8}',
            "risk_state_json": "{}",
            "metadata_json": '{"reward_unit": "quote_pnl"}',
        },
        {
            "experience_id": "valid-1",
            "symbol": "BTCUSDT",
            "decision": "backtest_trade",
            "outcome": "closed",
            "reward": 50.0,
            "features_json": '{"trend_strength": 8, "risk_amount_usd": 25}',
            "risk_state_json": '{"risk_amount_usd": 25}',
            "metadata_json": '{"reward_unit": "quote_pnl"}',
        },
    ]

    class Scorer:
        def __init__(self):
            self.updates = []

        def update_and_persist(self, symbol, features, reward):
            self.updates.append((symbol, features, reward))

        @property
        def n_samples_seen(self):
            return len(self.updates)

    worker.scorer = Scorer()
    worker._save_state = lambda: None

    processed = worker.run_once()

    assert processed == 3
    assert len(worker.scorer.updates) == 1
    symbol, features, reward = worker.scorer.updates[0]
    assert symbol == "BTCUSDT"
    assert reward == 2.0
    assert features == {"trend_strength": 8.0, "rl_confidence": 5.0}
    assert worker._processed == {"summary-1", "unscaled-1", "valid-1"}
