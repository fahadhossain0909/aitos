from datetime import datetime, timedelta, timezone

from aitos.intelligence.amt import TPOObservation, build_tpo_profile


def test_tpo_uses_distinct_time_brackets_not_trade_count():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    observations = [
        TPOObservation(start + timedelta(minutes=1), 100),
        TPOObservation(start + timedelta(minutes=2), 100),
        TPOObservation(start + timedelta(minutes=31), 101),
        TPOObservation(start + timedelta(minutes=32), 100),
    ]
    profile = build_tpo_profile(observations, tick_size=1, bracket_minutes=30)
    assert profile.bracket_count == 2
    assert profile.bins
    assert profile.poc == 100


def test_tpo_single_print_and_excess_candidates():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    observations = []
    for bracket in range(4):
        ts = start + timedelta(minutes=30 * bracket + 1)
        observations.extend([TPOObservation(ts, 100), TPOObservation(ts, 101)])
    observations.append(TPOObservation(start + timedelta(minutes=91), 102))
    profile = build_tpo_profile(observations, tick_size=1, bracket_minutes=30)
    assert 102 in profile.single_prints
