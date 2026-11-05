import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
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
            "decision": "backtest_trade",
            "outcome": "closed",
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
    assert (
        ContinualLearningWorker._reward_r_multiple(
            {
                "decision": "backtest_trade",
                "outcome": "closed",
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
    base = {
        "decision": "backtest_trade",
        "outcome": "closed",
        "reward": 1.0,
        "features_json": "{}",
        "risk_state_json": '{"risk_amount_usd": 10}',
        "metadata_json": '{"reward_unit": "quote_pnl"}',
    }
    invalid_risk = {**base, "risk_state_json": '{"risk_amount_usd": 0}'}
    invalid_reward = {**base, "reward": float("nan")}
    invalid_unit = {**base, "metadata_json": '{"reward_unit": "unknown"}'}
    assert ContinualLearningWorker._reward_r_multiple(invalid_risk) is None
    assert ContinualLearningWorker._reward_r_multiple(invalid_reward) is None
    assert ContinualLearningWorker._reward_r_multiple(invalid_unit) is None


def test_worker_trains_only_valid_normalized_trade_rewards():
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker._cursor_timestamp = "1970-01-01 00:00:00.000"
    worker._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    worker._processed = 0
    worker._legacy_processed = set()

    class Scorer:
        n_samples_seen = 0

        def __init__(self):
            self.updates = []
            self.applied = set()

        def update_experience_and_persist(
            self, experience_id, symbol, features, reward
        ):
            if experience_id in self.applied:
                return False
            self.applied.add(experience_id)
            self.updates.append((symbol, features, reward))
            self.n_samples_seen += 1
            return True

    worker.scorer = Scorer()
    worker._save_state = lambda: None
    worker._rows = lambda: [
        {
            "experience_id": "11111111-1111-1111-1111-111111111111",
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "symbol": "BTCUSDT",
            "decision": "backtest_summary",
            "outcome": "completed",
            "reward": 0.25,
            "features_json": '{"trend_strength": 8}',
            "risk_state_json": "{}",
            "metadata_json": "{}",
        },
        {
            "experience_id": "22222222-2222-2222-2222-222222222222",
            "timestamp": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "symbol": "BTCUSDT",
            "decision": "backtest_trade",
            "outcome": "closed",
            "reward": 50.0,
            "features_json": '{"trend_strength": 8}',
            "risk_state_json": "{}",
            "metadata_json": '{"reward_unit":"quote_pnl"}',
        },
        {
            "experience_id": "33333333-3333-3333-3333-333333333333",
            "timestamp": datetime(2026, 1, 3, tzinfo=timezone.utc),
            "symbol": "BTCUSDT",
            "decision": "backtest_trade",
            "outcome": "closed",
            "reward": 50.0,
            "features_json": '{"trend_strength": 8}',
            "risk_state_json": '{"risk_amount_usd":25}',
            "metadata_json": '{"reward_unit":"quote_pnl"}',
        },
    ]
    assert worker.run_once() == 3
    assert len(worker.scorer.updates) == 1
    _, features, reward = worker.scorer.updates[0]
    assert reward == 2.0
    assert features["rl_confidence"] == 5.0
    assert worker._cursor_experience_id.endswith("333333333333")


def test_worker_recovers_crash_after_model_checkpoint_before_cursor_save(tmp_path):
    model_path = str(tmp_path / "deep_value.pkl")
    state_path = tmp_path / "worker_state.json"
    row = {
        "experience_id": "crash-window-exp",
        "timestamp": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "symbol": "BTCUSDT",
        "decision": "backtest_trade",
        "outcome": "closed",
        "features_json": '{"trend_strength": 8}',
        "risk_state_json": "{}",
        "metadata_json": '{"reward_unit": "r_multiple"}',
        "reward": 1.5,
    }

    first = ContinualLearningWorker.__new__(ContinualLearningWorker)
    first.state_path = state_path
    first._cursor_timestamp = "1970-01-01 00:00:00.000"
    first._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    first._processed = 0
    first._legacy_processed = set()
    first.scorer = DeepValueRLScorer(state_path=model_path)
    first._save_state()
    first._rows = lambda: [row]

    def crash_before_cursor_checkpoint():
        raise OSError("simulated process crash before cursor checkpoint")

    first._save_state = crash_before_cursor_checkpoint
    try:
        first.run_once()
        assert False, "the simulated crash should escape run_once"
    except OSError as exc:
        assert "simulated process crash" in str(exc)

    # A new process reloads the old cursor but the model checkpoint already
    # contains the experience ID. Replaying the row must not train it twice.
    restarted = ContinualLearningWorker.__new__(ContinualLearningWorker)
    restarted.state_path = state_path
    restarted._cursor_timestamp = "1970-01-01 00:00:00.000"
    restarted._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    restarted._processed = 0
    restarted._legacy_processed = set()
    restarted.scorer = DeepValueRLScorer(state_path=model_path)
    restarted.scorer.load_state()
    restarted._load_state()
    restarted._rows = lambda: [row]

    assert restarted.run_once() == 1
    assert restarted.scorer.n_samples_seen == 1
    assert restarted._cursor_experience_id == "crash-window-exp"


def test_worker_cooperative_stop_waits_for_loop_exit_and_closes_client():
    class Client:
        closed = False

        def close(self):
            self.closed = True

    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker._stop = threading.Event()
    worker._closed = False
    worker._lock_handle = None
    worker.poll_seconds = 60
    worker.client = Client()
    started = threading.Event()

    def run_once():
        started.set()
        return 0

    worker.run_once = run_once
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(worker.run_forever)
        assert started.wait(timeout=1)
        worker.request_stop()
        task.result(timeout=2)

    assert worker._stop.is_set()
    assert worker.client.closed is True


def test_concurrent_scorers_merge_updates_without_lost_samples(tmp_path):
    path = str(tmp_path / "concurrent_model.pkl")

    def apply(index):
        scorer = DeepValueRLScorer(state_path=path)
        return scorer.update_experience_and_persist(
            f"concurrent-exp-{index}",
            "BTCUSDT",
            {"trend_strength": float(5 + index % 5)},
            0.25,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(apply, range(12)))

    assert all(results)
    restored = DeepValueRLScorer(state_path=path)
    assert restored.load_state() is True
    assert restored.n_samples_seen == 12

    # Re-delivery of an existing ID from a separate scorer is harmless.
    duplicate = DeepValueRLScorer(state_path=path)
    duplicate_result = duplicate.update_experience_and_persist(
        "concurrent-exp-3", "BTCUSDT", {"trend_strength": 8.0}, 0.25
    )
    assert duplicate_result is False
    assert duplicate.n_samples_seen == 12


def test_worker_partitions_regime_models_and_normalizes_regime_labels(tmp_path):
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker.scorer = DeepValueRLScorer(state_path=str(tmp_path / "deep_value.pkl"))
    worker._regime_scorers = {}

    trending = worker._scorer_for_regime("trending")
    ranging = worker._scorer_for_regime("ranging")

    assert trending is not ranging
    assert trending._state_path != ranging._state_path
    assert trending._state_path.parent.name == "regimes"
    assert (
        ContinualLearningWorker._row_regime(
            {"metadata_json": '{"market_regime":"High Volatility"}'}
        )
        == "high_volatility"
    )

def test_paper_and_live_outcomes_require_explicit_reward_units():
    paper_r = {
        "source": "paper",
        "decision": "LONG",
        "outcome": "closed",
        "reward": 0.8,
        "metadata_json": '{"reward_unit": "r_multiple"}',
        "risk_state_json": '{"risk_amount_usd": 5.0}',
    }
    live_quote = {
        "source": "live",
        "decision": "SHORT",
        "outcome": "closed",
        "reward": 4.0,
        "metadata_json": '{"reward_unit": "quote_pnl"}',
        "risk_state_json": '{"risk_amount_usd": 8.0}',
    }
    legacy_paper = {
        "source": "paper",
        "decision": "LONG",
        "outcome": "closed",
        "reward": 4.0,
        "metadata_json": "{}",
        "risk_state_json": '{"risk_amount_usd": 8.0}',
    }

    assert ContinualLearningWorker._reward_r_multiple(paper_r) == 0.8
    assert ContinualLearningWorker._reward_r_multiple(live_quote) == 0.5
    assert ContinualLearningWorker._reward_r_multiple(legacy_paper) is None


def test_worker_trains_on_normalized_paper_live_outcomes():
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker._cursor_timestamp = "1970-01-01 00:00:00.000"
    worker._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    worker._processed = 0
    worker._legacy_processed = set()

    class Scorer:
        n_samples_seen = 0

        def __init__(self):
            self.rewards = []

        def update_experience_and_persist(
            self, experience_id, symbol, features, reward
        ):
            self.rewards.append((symbol, reward))
            self.n_samples_seen += 1
            return True

    worker.scorer = Scorer()
    worker._save_state = lambda: None
    worker._rows = lambda: [
        {
            "experience_id": "paper-outcome-1",
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "source": "paper",
            "symbol": "BTCUSDT",
            "decision": "LONG",
            "outcome": "closed",
            "reward": 0.8,
            "features_json": '{"trend_strength": 8}',
            "risk_state_json": '{"risk_amount_usd": 5.0}',
            "metadata_json": '{"reward_unit": "r_multiple"}',
        },
        {
            "experience_id": "paper-outcome-2",
            "timestamp": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "source": "paper",
            "symbol": "ETHUSDT",
            "decision": "LONG",
            "outcome": "closed",
            "reward": 4.0,
            "features_json": '{"trend_strength": 7}',
            "risk_state_json": '{"risk_amount_usd": 8.0}',
            "metadata_json": "{}",
        },
    ]

    assert worker.run_once() == 2
    assert worker.scorer.rewards == [("BTCUSDT", 0.8)]

def test_worker_query_includes_backtest_paper_and_live_experiences():
    from types import SimpleNamespace

    class FakeClient:
        def __init__(self):
            self.sql = ""

        def query(self, sql, parameters):
            self.sql = sql
            return SimpleNamespace(column_names=[], result_rows=[])

    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker.client = FakeClient()
    worker._cursor_timestamp = "1970-01-01 00:00:00.000"
    worker._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    worker.batch_limit = 5000

    assert worker._rows() == []
    assert "source IN ('backtest', 'paper', 'live')" in worker.client.sql
    assert "strategy_version" in worker.client.sql
