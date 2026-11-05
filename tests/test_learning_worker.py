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


def test_worker_processes_12000_experiences_across_bounded_batches(tmp_path):
    """Large synthetic replay must advance across bounded worker batches."""
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker.state_path = tmp_path / "worker_state.json"
    worker._cursor_timestamp = "1970-01-01 00:00:00.000"
    worker._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    worker._processed = 0
    worker._legacy_processed = set()
    worker.batch_limit = 5000
    worker._regime_scorers = {}

    class Scorer:
        n_samples_seen = 0

        def __init__(self):
            self.seen = set()

        def update_experience_and_persist(
            self, experience_id, symbol, features, reward
        ):
            if experience_id in self.seen:
                return False
            self.seen.add(experience_id)
            self.n_samples_seen += 1
            return True

    worker.scorer = Scorer()
    worker._reward_calibrator = __import__(
        "aitos.learning.regime_feedback", fromlist=["RegimeRewardCalibrator"]
    ).RegimeRewardCalibrator()
    rows = []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(12_000):
        rows.append(
            {
                "experience_id": f"00000000-0000-0000-0000-{index + 1:012d}",
                "timestamp": base.replace(microsecond=index % 1_000_000),
                "symbol": "BTCUSDT",
                "decision": "backtest_trade",
                "outcome": "closed",
                "features_json": '{"trend_strength": 8}',
                "risk_state_json": "{}",
                "metadata_json": '{"reward_unit": "r_multiple", "regime": "unknown"}',
                "reward": 1.0,
            }
        )

    offset = 0

    def fetch_page():
        nonlocal offset
        page = rows[offset : offset + worker.batch_limit]
        offset += len(page)
        return page

    worker._rows = fetch_page
    # Avoid 12,000 filesystem writes in this scale-only test; persistence and
    # crash-window behavior are covered by the dedicated recovery tests above.
    worker._save_state = lambda: None

    assert worker.run_once() == 5_000
    assert worker.run_once() == 10_000
    assert worker.run_once() == 12_000
    assert worker.scorer.n_samples_seen == 12_000
    assert len(worker.scorer.seen) == 12_000
    assert offset == 12_000


def test_learning_worker_singleton_file_lock_rejects_second_owner(tmp_path):
    first = ContinualLearningWorker.__new__(ContinualLearningWorker)
    first.state_path = tmp_path / "worker_state.json"
    first._lock_handle = None
    first._acquire_singleton_lock()

    second = ContinualLearningWorker.__new__(ContinualLearningWorker)
    second.state_path = first.state_path
    second._lock_handle = None
    try:
        try:
            second._acquire_singleton_lock()
            assert False, "a second worker must not acquire the same state lock"
        except RuntimeError as exc:
            assert "another continual-learning worker" in str(exc)
    finally:
        first._release_singleton_lock()

    second._acquire_singleton_lock()
    second._release_singleton_lock()


def test_learning_worker_singleton_lock_rejects_second_process(tmp_path):
    state_path = tmp_path / "worker_state.json"
    ready_path = tmp_path / "worker.locked"
    release_path = tmp_path / "worker.release"
    child_code = """
import sys
import time
from pathlib import Path
from aitos.learning.worker import ContinualLearningWorker

worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
worker.state_path = Path(sys.argv[1])
worker._lock_handle = None
worker._acquire_singleton_lock()
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
worker._release_singleton_lock()
if not Path(sys.argv[3]).exists():
    raise SystemExit(3)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(state_path),
            str(ready_path),
            str(release_path),
        ],
        cwd=os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert ready_path.exists(), process.communicate(timeout=2)

        contender = ContinualLearningWorker.__new__(ContinualLearningWorker)
        contender.state_path = state_path
        contender._lock_handle = None
        try:
            contender._acquire_singleton_lock()
            contender._release_singleton_lock()
            assert False, "a second process must not acquire the worker state lock"
        except RuntimeError as exc:
            assert "another continual-learning worker" in str(exc)
    finally:
        release_path.write_text("release", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, (stdout, stderr)

    recovered = ContinualLearningWorker.__new__(ContinualLearningWorker)
    recovered.state_path = state_path
    recovered._lock_handle = None
    recovered._acquire_singleton_lock()
    recovered._release_singleton_lock()


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


def test_worker_processes_12000_experiences_across_bounded_batches(tmp_path):
    """Large synthetic replay must advance across bounded worker batches."""
    worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
    worker.state_path = tmp_path / "worker_state.json"
    worker._cursor_timestamp = "1970-01-01 00:00:00.000"
    worker._cursor_experience_id = "00000000-0000-0000-0000-000000000000"
    worker._processed = 0
    worker._legacy_processed = set()
    worker.batch_limit = 5000
    worker._regime_scorers = {}

    class Scorer:
        n_samples_seen = 0

        def __init__(self):
            self.seen = set()

        def update_experience_and_persist(
            self, experience_id, symbol, features, reward
        ):
            if experience_id in self.seen:
                return False
            self.seen.add(experience_id)
            self.n_samples_seen += 1
            return True

    worker.scorer = Scorer()
    worker._reward_calibrator = __import__(
        "aitos.learning.regime_feedback", fromlist=["RegimeRewardCalibrator"]
    ).RegimeRewardCalibrator()
    rows = []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(12_000):
        rows.append(
            {
                "experience_id": f"00000000-0000-0000-0000-{index + 1:012d}",
                "timestamp": base.replace(microsecond=index % 1_000_000),
                "symbol": "BTCUSDT",
                "decision": "backtest_trade",
                "outcome": "closed",
                "features_json": '{"trend_strength": 8}',
                "risk_state_json": "{}",
                "metadata_json": '{"reward_unit": "r_multiple", "regime": "unknown"}',
                "reward": 1.0,
            }
        )

    offset = 0

    def fetch_page():
        nonlocal offset
        page = rows[offset : offset + worker.batch_limit]
        offset += len(page)
        return page

    worker._rows = fetch_page
    # Avoid 12,000 filesystem writes in this scale-only test; persistence and
    # crash-window behavior are covered by the dedicated recovery tests above.
    worker._save_state = lambda: None

    assert worker.run_once() == 5_000
    assert worker.run_once() == 10_000
    assert worker.run_once() == 12_000
    assert worker.scorer.n_samples_seen == 12_000
    assert len(worker.scorer.seen) == 12_000
    assert offset == 12_000


def test_learning_worker_singleton_file_lock_rejects_second_owner(tmp_path):
    first = ContinualLearningWorker.__new__(ContinualLearningWorker)
    first.state_path = tmp_path / "worker_state.json"
    first._lock_handle = None
    first._acquire_singleton_lock()

    second = ContinualLearningWorker.__new__(ContinualLearningWorker)
    second.state_path = first.state_path
    second._lock_handle = None
    try:
        try:
            second._acquire_singleton_lock()
            assert False, "a second worker must not acquire the same state lock"
        except RuntimeError as exc:
            assert "another continual-learning worker" in str(exc)
    finally:
        first._release_singleton_lock()

    second._acquire_singleton_lock()
    second._release_singleton_lock()


def test_learning_worker_singleton_lock_rejects_second_process(tmp_path):
    state_path = tmp_path / "worker_state.json"
    ready_path = tmp_path / "worker.locked"
    release_path = tmp_path / "worker.release"
    child_code = """
import sys
import time
from pathlib import Path
from aitos.learning.worker import ContinualLearningWorker

worker = ContinualLearningWorker.__new__(ContinualLearningWorker)
worker.state_path = Path(sys.argv[1])
worker._lock_handle = None
worker._acquire_singleton_lock()
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
worker._release_singleton_lock()
if not Path(sys.argv[3]).exists():
    raise SystemExit(3)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_code,
            str(state_path),
            str(ready_path),
            str(release_path),
        ],
        cwd=os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert ready_path.exists(), process.communicate(timeout=2)

        contender = ContinualLearningWorker.__new__(ContinualLearningWorker)
        contender.state_path = state_path
        contender._lock_handle = None
        try:
            contender._acquire_singleton_lock()
            contender._release_singleton_lock()
            assert False, "a second process must not acquire the worker state lock"
        except RuntimeError as exc:
            assert "another continual-learning worker" in str(exc)
    finally:
        release_path.write_text("release", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, (stdout, stderr)

    recovered = ContinualLearningWorker.__new__(ContinualLearningWorker)
    recovered.state_path = state_path
    recovered._lock_handle = None
    recovered._acquire_singleton_lock()
    recovered._release_singleton_lock()
