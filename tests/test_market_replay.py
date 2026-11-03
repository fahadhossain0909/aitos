from datetime import datetime, timezone

from aitos.backtest.replay import MarketReplay


class Event:
    def __init__(self, timestamp: datetime, value: int) -> None:
        self.timestamp = timestamp
        self.value = value


def test_replay_orders_events_by_timestamp_and_reports_stats() -> None:
    t1 = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
    replay = MarketReplay([Event(t2, 2), Event(t1, 1)])
    seen = []

    stats = replay.run(lambda event: seen.append(event.value))

    assert seen == [1, 2]
    assert stats.events_seen == 2
    assert stats.events_emitted == 2
    assert stats.start_time == t1
    assert stats.end_time == t2


def test_empty_replay() -> None:
    stats = MarketReplay([]).run(lambda _: None)
    assert stats.events_seen == 0
    assert stats.events_emitted == 0
    assert stats.start_time is None
    assert stats.end_time is None
