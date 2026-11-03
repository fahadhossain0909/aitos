from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aitos.learning.validation import CandidateValidator, ValidationPolicy


def _events(days=250):
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            timestamp=base + timedelta(days=i), value=float(i + 1), fields={}
        )
        for i in range(days)
    ]


def _strategy(event, execution):
    return None


def _mark_price(event):
    return event.value


def test_walk_forward_gate_is_executed_for_candidate():
    validator = CandidateValidator(
        ValidationPolicy(
            min_total_return=-1.0,
            min_sharpe=-100.0,
            min_trades=0,
            require_improvement_over_champion=False,
            require_walk_forward=True,
            train_days=30,
            test_days=10,
            holdout_days=10,
            min_positive_windows=0,
        )
    )
    result = validator.evaluate(_events(), _strategy, _mark_price)
    assert result.walk_forward is not None
    assert result.walk_forward.windows


def test_walk_forward_rejects_insufficient_history():
    validator = CandidateValidator(
        ValidationPolicy(
            min_total_return=-1.0,
            min_sharpe=-100.0,
            min_trades=0,
            require_improvement_over_champion=False,
            require_walk_forward=True,
            train_days=180,
            test_days=30,
            holdout_days=30,
        )
    )
    result = validator.evaluate(_events(20), _strategy, _mark_price)
    assert not result.passed
    assert "walk-forward" in result.reason
