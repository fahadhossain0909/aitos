import asyncio
from datetime import datetime, timezone

from aitos.intelligence.deep_rl_policy import DeepValueRLScorer
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


def test_numeric_features_rejects_non_finite_values():
    result = ContinualLearningWorker._numeric_features(
        {"valid": 3.0, "nan": float("nan"), "infinite": float("inf")}
    )
    assert result == {"valid": 3.0, "rl_confidence": 5.0}


def test_cursor_state_round_trip(tmp_path):
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker.state_path = tmp_path / "worker_state.json"
    worker._cursor_timestamp = "2026-01-01 00:00:00.000"
    worker._cursor_experience_id = "11111111-1111-1111-1111-111111111111"
    worker._processed = 17
    worker._legacy_processed = set()
    worker.scorer = type("Scorer", (), {"n_samples_seen": 12})()
    worker._save_state()

    restored = ContinualLearningWorker.__new__(ContinualLearningWorker)
    restored.state_path = worker.state_path
    restored._cursor_timestamp = "1970-01-01 00:00:00.000"
    restored._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    restored._processed = 0
    restored._legacy_processed = set()
    restored._load_state()
    assert restored._cursor_timestamp == worker._cursor_timestamp
    assert restored._cursor_experience_id == worker._cursor_experience_id
    assert restored._processed == 17


def test_worker_advances_keyset_cursor_and_skips_poison_rows(tmp_path):
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker.state_path = tmp_path / "worker_state.json"
    worker._cursor_timestamp = "1970-01-01 00:00:00.000"
    worker._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    worker._processed = 0
    worker._legacy_processed = set()

    class Scorer:
        n_samples_seen = 0
        seen = set()

        def update_experience_and_persist(
            self, experience_id, symbol, features, reward
        ):
            if experience_id in self.seen:
                return False
            self.seen.add(experience_id)
            self.n_samples_seen += 1
            return True

    worker.scorer = Scorer()
    worker._rows = lambda: [
        {
            "experience_id": "11111111-1111-1111-1111-111111111111",
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "symbol": "BTCUSDT",
            "features_json": '{"trend_strength": 8}',
            "risk_state_json": "{}",
            "metadata_json": '{"reward_unit": "r_multiple"}',
            "reward": 1.25,
        },
        {
            "experience_id": "22222222-2222-2222-2222-222222222222",
            "timestamp": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "symbol": "ETHUSDT",
            "features_json": "invalid",
            "risk_state_json": "{}",
            "metadata_json": "{}",
            "reward": 0,
        },
    ]
    assert worker.run_once() == 2
    assert worker._cursor_experience_id.endswith("222222222222")
    assert worker.scorer.n_samples_seen == 1


def test_deep_rl_experience_update_is_idempotent_after_reload(tmp_path):
    path = str(tmp_path / "deep_value.pkl")
    first = DeepValueRLScorer(state_path=path)
    assert (
        first.update_experience_and_persist(
            "exp-1", "BTCUSDT", {"trend_strength": 8.0}, 1.0
        )
        is True
    )
    assert first.n_samples_seen == 1

    second = DeepValueRLScorer(state_path=path)
    assert second.load_state() is True
    assert (
        second.update_experience_and_persist(
            "exp-1", "BTCUSDT", {"trend_strength": 8.0}, 1.0
        )
        is False
    )
    assert second.n_samples_seen == 1


def test_backtest_rewards_are_normalized_to_r_multiples():
    assert (
        ContinualLearningWorker._reward_r_multiple(
            {
                "reward": 50.0,
                "features_json": "{}",
                "risk_state_json": '{"risk_amount_usd": 25.0}',
                "metadata_json": '{"reward_unit": "quote_pnl"}',
            }
        )
        == 2.0
    )
    assert (
        ContinualLearningWorker._reward_r_multiple(
            {
                "reward": -0.75,
                "features_json": "{}",
                "risk_state_json": "{}",
                "metadata_json": '{"reward_unit": "r_multiple"}',
            }
        )
        == -0.75
    )
    assert (
        ContinualLearningWorker._reward_r_multiple(
            {
                "reward": 50.0,
                "features_json": "{}",
                "risk_state_json": "{}",
                "metadata_json": "{}",
            }
        )
        is None
    )


def test_outcome_and_risk_fields_are_not_training_features():
    result = ContinualLearningWorker._numeric_features(
        '{"trend_strength": 7, "risk_amount_usd": 100, "pnl": 20, "reward": 2}'
    )
    assert result == {"trend_strength": 7.0, "rl_confidence": 5.0}


def test_scorer_refreshes_checkpoint_written_by_another_process(tmp_path):
    path = str(tmp_path / "shared_model.pkl")
    writer = DeepValueRLScorer(state_path=path)
    reader = DeepValueRLScorer(state_path=path)
    assert reader.n_samples_seen == 0
    writer.update_experience_and_persist(
        "external-exp", "BTCUSDT", {"trend_strength": 8.0}, 1.0
    )
    asyncio.run(reader.score("BTCUSDT", {"trend_strength": 8.0}))
    assert reader.n_samples_seen == 1
