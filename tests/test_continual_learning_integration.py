from datetime import datetime, timedelta, timezone

from aitos.backtest.cli import HistoricalEvent
from aitos.learning.experience import ExperienceRecord
from aitos.learning.model_registry import ModelArtifact, ModelRegistry
from aitos.learning.validation import CandidateValidator, ValidationPolicy
from aitos.learning.walk_forward import WalkForwardValidator


def _events(days: int = 10):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        HistoricalEvent(start + timedelta(days=i), 100.0 + i, {"symbol": "BTCUSDT"})
        for i in range(days)
    ]


def _strategy(event, execution):
    if not getattr(execution, "bought", False):
        execution.execute("buy", 1.0, event.price)
        execution.bought = True


def test_experience_record_is_serializable_and_source_bound():
    record = ExperienceRecord(
        timestamp=datetime.now(timezone.utc),
        source="paper",
        symbol="BTCUSDT",
        decision="LONG",
        confidence=0.8,
    )
    payload = record.as_dict()
    assert payload["source"] == "paper"
    assert payload["symbol"] == "BTCUSDT"


def test_candidate_validator_uses_canonical_backtest():
    result = CandidateValidator(ValidationPolicy(min_trades=0)).evaluate(
        _events(), _strategy, lambda e: e.price
    )
    assert result.candidate.metrics.initial_equity == 10_000.0


def test_walk_forward_keeps_locked_holdout_out_of_windows():
    events = _events(400)
    start = events[0].timestamp
    end = events[-1].timestamp + timedelta(days=1)
    result = WalkForwardValidator(min_positive_windows=0).evaluate(
        events,
        _strategy,
        lambda e: e.price,
        start,
        end,
        train_days=180,
        test_days=30,
        holdout_days=30,
    )
    assert result.windows
    assert result.locked_holdout is not None
    assert all(w.test_end <= end - timedelta(days=30) for w in result.windows)


def test_model_registry_promotes_only_candidates(tmp_path):
    registry = ModelRegistry(str(tmp_path / "registry.json"))
    registry.register(
        ModelArtifact(name="strategy", version="v1", kind="rule", status="candidate")
    )
    promoted = registry.promote("strategy", "v1")
    assert promoted.status == "champion"
