from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitos.learning.validation import CandidateValidator, ValidationPolicy


def test_candidate_metrics_exclude_locked_holdout(monkeypatch):
    captured = []

    class FakeBacktestEngine:
        def __init__(self, *args):
            pass

        def run(self, events, strategy, mark_price):
            captured.append(list(events))
            metrics = SimpleNamespace(
                total_return=0.1,
                max_drawdown=0.05,
                sharpe=1.2,
                trades=20,
                initial_equity=10000.0,
                final_equity=11000.0,
            )
            return SimpleNamespace(metrics=metrics)

    class FakeWalkForwardValidator:
        def __init__(self, **kwargs):
            pass

        def evaluate(self, events, strategy, mark_price, **kwargs):
            return SimpleNamespace(
                passed=True,
                locked_holdout=SimpleNamespace(metrics=object()),
                windows=[object()],
                reason="walk-forward and locked holdout passed",
            )

    monkeypatch.setattr("aitos.learning.validation.BacktestEngine", FakeBacktestEngine)
    monkeypatch.setattr(
        "aitos.learning.validation.WalkForwardValidator", FakeWalkForwardValidator
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        SimpleNamespace(timestamp=start + timedelta(days=index)) for index in range(100)
    ]
    policy = ValidationPolicy(
        train_days=30, test_days=20, holdout_days=30, min_trades=1
    )

    result = CandidateValidator(policy).evaluate(
        events, lambda rows: None, lambda event: 100.0
    )

    assert result.passed
    assert len(captured[0]) == 70
    assert max(event.timestamp for event in captured[0]) < start + timedelta(days=70)
    # The full chronological dataset is only passed to the separate,
    # non-tuning walk-forward evaluator, never the candidate threshold run.


def test_candidate_validator_rejects_naive_timestamps():
    from datetime import datetime

    import pytest

    events = [SimpleNamespace(timestamp=datetime(2026, 1, 1))]
    with pytest.raises(ValueError, match="timezone-aware"):
        CandidateValidator().evaluate(events, lambda rows: None, lambda event: 1.0)
