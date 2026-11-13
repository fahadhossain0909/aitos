from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.event_study import (
    EventStudyObservation,
    estimate_event_beta,
)


AS_OF = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def observation(index, *, event_time=None, outcome_time=None, signal=None, result=None):
    event_time = event_time or AS_OF - timedelta(days=2)
    outcome_time = outcome_time or AS_OF - timedelta(days=1)
    signal = signal if signal is not None else (index - 15) / 15
    result = result if result is not None else signal * 0.02
    return EventStudyObservation(
        asset="BTCUSDT",
        event_type="earnings",
        horizon="1h",
        event_time=event_time,
        outcome_available_at=outcome_time,
        event_signal=signal,
        forward_return=result,
    )


def test_event_beta_uses_only_point_in_time_available_outcomes():
    observations = [observation(i) for i in range(40)]
    observations.append(
        observation(
            41,
            event_time=AS_OF - timedelta(hours=1),
            outcome_time=AS_OF + timedelta(hours=1),
            signal=1.0,
            result=-1.0,
        )
    )
    result = estimate_event_beta(
        observations,
        asset="BTCUSDT",
        event_type="earnings",
        horizon="1h",
        as_of=AS_OF,
        min_samples=30,
    )
    assert result.sample_count == 40
    assert result.beta > 0
    assert result.status == "estimated"


def test_insufficient_history_defaults_to_zero_beta():
    result = estimate_event_beta(
        [observation(i) for i in range(10)],
        asset="BTCUSDT",
        event_type="earnings",
        horizon="1h",
        as_of=AS_OF,
    )
    assert result.beta == 0.0
    assert result.sample_count == 10
    assert result.status == "insufficient_history"


def test_future_events_are_excluded_even_if_outcome_timestamp_is_old():
    result = estimate_event_beta(
        [
            observation(
                i,
                event_time=AS_OF + timedelta(minutes=1),
                outcome_time=AS_OF + timedelta(hours=2),
            )
            for i in range(40)
        ],
        asset="BTCUSDT",
        event_type="earnings",
        horizon="1h",
        as_of=AS_OF,
    )
    assert result.sample_count == 0
    assert result.beta == 0.0


def test_wrong_asset_event_type_and_horizon_are_excluded():
    result = estimate_event_beta(
        [observation(i) for i in range(40)],
        asset="ETHUSDT",
        event_type="earnings",
        horizon="1h",
        as_of=AS_OF,
    )
    assert result.sample_count == 0
    assert result.beta == 0.0
