from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from aitos.learning.ablation import HistoricalAblationRunner


@dataclass(frozen=True)
class Event:
    timestamp: datetime
    price: float
    fields: dict


def make_events(days=8):
    start = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    events = []
    for day in range(days):
        base = start + timedelta(days=day)
        events.extend(
            [
                Event(base, 100.0, {"action": "open", "side": "LONG"}),
                Event(base + timedelta(hours=6), 110.0, {"action": "close", "side": "LONG"}),
                Event(base + timedelta(hours=7), 110.0, {"action": "open", "side": "SHORT"}),
                Event(base + timedelta(hours=12), 100.0, {"action": "close", "side": "SHORT"}),
            ]
        )
    return events


def both_sides_strategy(event, execution):
    side = event.fields["side"]
    if event.fields["action"] == "open":
        execution.execute("buy" if side == "LONG" else "sell", 1.0, event.price)
    else:
        execution.execute("sell" if side == "LONG" else "buy", 1.0, event.price)


def long_only_strategy(event, execution):
    if event.fields["side"] == "LONG":
        both_sides_strategy(event, execution)


def test_historical_ablation_runs_canonical_replay_walk_forward_and_side_metrics():
    report = HistoricalAblationRunner(
        initial_cash=10_000.0,
        fee_rate=0.0,
        train_days=2,
        test_days=1,
        holdout_days=1,
    ).run(
        make_events(),
        {"baseline": both_sides_strategy, "long_only": long_only_strategy},
        lambda event: event.price,
    )

    assert report.baseline == "baseline"
    assert set(report.variants) == {"baseline", "long_only"}
    baseline = report.variants["baseline"]
    assert baseline.replay.metrics.trades == 16
    assert baseline.directional_metrics["LONG"]["trade_count"] == 8.0
    assert baseline.directional_metrics["SHORT"]["trade_count"] == 8.0
    assert baseline.unclassified_trade_count == 0
    assert baseline.walk_forward.windows
    assert baseline.walk_forward.locked_holdout is not None
    assert baseline.walk_forward.passed
    assert report.deltas_vs_baseline["long_only"]["total_return"] < 0


def test_historical_ablation_requires_baseline_and_multiple_variants():
    runner = HistoricalAblationRunner(train_days=2, test_days=1, holdout_days=1)
    with pytest.raises(ValueError, match="baseline strategy"):
        runner.run(make_events(), {"long_only": long_only_strategy}, lambda event: event.price)
    with pytest.raises(ValueError, match="at least one variant"):
        runner.run(make_events(), {"baseline": both_sides_strategy}, lambda event: event.price)
