import math
from datetime import datetime, timedelta, timezone

import pytest

from aitos.models.market import TradeSide, TradeTick
from aitos.statistics.engine import AStatEngine
from aitos.statistics.hawkes import HawkesMLEFit
from aitos.statistics import hawkes_runtime


def _trades(count=100):
    start = datetime(2026, 11, 8, tzinfo=timezone.utc)
    return [
        TradeTick(
            symbol="BTCUSDT",
            trade_id=index + 1,
            price=100.0,
            quantity=0.01,
            side=TradeSide.BUY if index % 2 else TradeSide.SELL,
            timestamp=start + timedelta(seconds=index),
        )
        for index in range(count)
    ]


class _Exchange:
    def __init__(self, trades):
        self.trades = trades

    async def fetch_recent_trades(self, symbol, limit=500):
        assert symbol == "BTCUSDT"
        assert limit == 100
        return self.trades


def _fit(horizon):
    return HawkesMLEFit(
        baseline_intensities=(0.4, 0.4),
        excitation_matrix=((0.1, 0.02), (0.02, 0.1)),
        decay=1.0,
        branching_ratio=0.12,
        log_likelihood=-10.0,
        iterations=10,
        events=80,
        horizon=horizon,
        converged=True,
        gradient_norm=0.001,
    )


@pytest.mark.asyncio
async def test_hawkes_bootstrap_registers_only_after_oos_acceptance(monkeypatch):
    monkeypatch.setattr(
        hawkes_runtime,
        "fit_marked_hawkes_mle",
        lambda times, marks, **kwargs: _fit(kwargs["horizon"]),
    )
    monkeypatch.setattr(hawkes_runtime, "_holdout_gain", lambda *args: 0.25)
    engine = AStatEngine()

    results = await hawkes_runtime.bootstrap_hawkes_models(
        _Exchange(_trades()),
        engine,
        ["BTCUSDT"],
        sample_limit=100,
        min_events=100,
        min_mark_events=10,
        decay_grid=(0.1, 0.5),
    )

    assert len(results) == 1
    assert results[0].status == "accepted"
    assert results[0].oos_gain_per_event == 0.25
    assert engine.has_hawkes_model("BTCUSDT")
    last_timestamp = _trades()[-1].timestamp.timestamp()
    status = engine.hawkes_model_status()["BTCUSDT"]
    assert status["training_events"] == 80
    assert status["last_event_timestamp"] == pytest.approx(last_timestamp)
    snapshot = engine.observe_marked_event("BTCUSDT", last_timestamp + 1, 1)
    assert snapshot.timestamp == last_timestamp + 1


@pytest.mark.asyncio
async def test_hawkes_bootstrap_rejects_model_that_loses_to_poisson_oos(monkeypatch):
    monkeypatch.setattr(
        hawkes_runtime,
        "fit_marked_hawkes_mle",
        lambda times, marks, **kwargs: _fit(kwargs["horizon"]),
    )
    monkeypatch.setattr(hawkes_runtime, "_holdout_gain", lambda *args: -0.1)
    engine = AStatEngine()

    results = await hawkes_runtime.bootstrap_hawkes_models(
        _Exchange(_trades()),
        engine,
        ["BTCUSDT"],
        sample_limit=100,
        min_events=100,
        min_mark_events=10,
        decay_grid=(0.1, 0.5),
    )

    assert results[0].status == "rejected"
    assert results[0].reason == "oos_did_not_beat_poisson_baseline"
    assert not engine.has_hawkes_model("BTCUSDT")


@pytest.mark.asyncio
async def test_hawkes_bootstrap_skips_insufficient_history_without_registration():
    engine = AStatEngine()
    results = await hawkes_runtime.bootstrap_hawkes_models(
        _Exchange(_trades(30)),
        engine,
        ["BTCUSDT"],
        sample_limit=100,
        min_events=100,
    )
    assert results[0].status == "skipped"
    assert results[0].reason == "insufficient_events"
    assert not engine.has_hawkes_model("BTCUSDT")


def test_hawkes_warmup_events_make_duplicate_millisecond_times_strictly_ordered():
    trades = _trades(4)
    same_time = trades[0].timestamp
    trades[1] = TradeTick(
        symbol="BTCUSDT",
        trade_id=2,
        price=100.0,
        quantity=0.01,
        side=TradeSide.BUY,
        timestamp=same_time,
    )
    # Strict ordering is required because the exchange timestamps are millisecond-granular.
    times, marks = hawkes_runtime._strict_event_times(trades)
    assert all(left < right for left, right in zip(times, times[1:]))
    assert len(times) == len(marks)


def test_invalid_hawkes_warmup_does_not_replace_existing_filter():
    engine = AStatEngine()
    fit = _fit(80.0)
    engine.register_hawkes_model("BTCUSDT", fit, warmup_events=[(100.0, 1)])
    with pytest.raises(ValueError):
        engine.register_hawkes_model(
            "BTCUSDT", fit, warmup_events=[(101.0, 1), (100.0, 0)]
        )
    assert engine.last_hawkes_timestamp("BTCUSDT") == 100.0


def test_hawkes_oos_gain_integrates_excitation_only_over_holdout_window():
    fit = _fit(5.0)
    train_times = [0.0, 1.0]
    train_marks = [0, 1]
    holdout_times = [2.0, 3.0]
    holdout_marks = [0, 1]
    boundary = 1.5
    end = 4.0

    online = hawkes_runtime.MarkedHawkesOnlineFilter(fit)
    for timestamp, mark in zip(train_times, train_marks):
        online.observe(timestamp, mark)
    event_log_likelihood = sum(
        math.log(online.observe(timestamp, mark).intensities[mark])
        for timestamp, mark in zip(holdout_times, holdout_marks)
    )
    duration = end - boundary
    counts = [train_marks.count(mark) for mark in (0, 1)]
    baseline = [count / boundary for count in counts]
    poisson_log_likelihood = sum(math.log(baseline[mark]) for mark in holdout_marks)

    source_integrals = []
    for source in (0, 1):
        total = 0.0
        for timestamp, mark in zip(
            train_times + holdout_times, train_marks + holdout_marks
        ):
            if mark != source or timestamp >= end:
                continue
            at_boundary = (
                math.exp(-fit.decay * (boundary - timestamp))
                if timestamp < boundary
                else 1.0
            )
            at_end = math.exp(-fit.decay * (end - timestamp))
            total += max(0.0, at_boundary - at_end)
        source_integrals.append(total)
    excitation_integral = sum(
        fit.excitation_matrix[target][source] * source_integrals[source]
        for target in (0, 1)
        for source in (0, 1)
    )
    hawkes_integral = sum(fit.baseline_intensities) * duration + excitation_integral
    poisson_integral = sum(baseline) * duration
    expected = (
        event_log_likelihood
        - hawkes_integral
        - poisson_log_likelihood
        + poisson_integral
    ) / len(holdout_marks)

    actual = hawkes_runtime._holdout_gain(
        fit,
        train_times,
        train_marks,
        holdout_times,
        holdout_marks,
        boundary,
        end,
    )
    assert actual == pytest.approx(expected)
