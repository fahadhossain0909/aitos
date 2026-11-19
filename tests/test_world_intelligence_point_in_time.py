from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.point_in_time import (
    EventRevision,
    MarketBarRevision,
    build_point_in_time_labels,
    to_event_impact_training_rows,
)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def event(revision_id="v1", *, known_hours=0.5, sentiment=0.4):
    published_at = BASE
    known_at = BASE + timedelta(hours=known_hours)
    return EventRevision(
        event_id="event-1",
        revision_id=revision_id,
        asset="BTCUSDT",
        event_type="macro",
        published_at=published_at,
        known_at=known_at,
        sentiment=sentiment,
        severity=0.8,
        novelty=0.7,
        confidence=0.9,
        source_reliability=0.95,
        regime="trend",
        beta=0.3,
    )


def bar(
    open_hour,
    *,
    close_hour=None,
    available_hour=None,
    revision_id="v1",
    open_price=100.0,
    close_price=101.0,
):
    close_hour = open_hour + 1 if close_hour is None else close_hour
    available_hour = close_hour if available_hour is None else available_hour
    return MarketBarRevision(
        asset="BTCUSDT",
        revision_id=revision_id,
        open_time=BASE + timedelta(hours=open_hour),
        close_time=BASE + timedelta(hours=close_hour),
        available_at=BASE + timedelta(hours=available_hour),
        open_price=open_price,
        close_price=close_price,
    )


def build(events, bars, *, as_of_hours=10):
    return build_point_in_time_labels(
        events,
        bars,
        horizons={"1h": timedelta(hours=1), "4h": timedelta(hours=4)},
        as_of=BASE + timedelta(hours=as_of_hours),
    )


def test_label_uses_first_known_event_revision_and_first_bar_revision():
    events = [
        event("v1", sentiment=0.4),
        event("v2", known_hours=3, sentiment=-0.9),
    ]
    bars = [
        bar(1, close_hour=2, available_hour=2, close_price=101.0),
        bar(1, close_hour=2, available_hour=5, revision_id="v2", close_price=80.0),
        bar(2, close_hour=3, available_hour=3, open_price=101.0, close_price=102.0),
        bar(3, close_hour=4, available_hour=4, open_price=102.0, close_price=103.0),
        bar(4, close_hour=5, available_hour=5, open_price=103.0, close_price=104.0),
        bar(5, close_hour=6, available_hour=6, open_price=104.0, close_price=105.0),
    ]
    report = build(events, bars)
    one_hour = next(label for label in report.labels if label.horizon == "1h")
    assert one_hour.sentiment == 0.4
    assert one_hour.event_time == BASE + timedelta(hours=0.5)
    assert one_hour.entry_price == 100.0
    assert one_hour.exit_price == 101.0
    assert one_hour.forward_return == pytest.approx(0.01)
    assert one_hour.direction_up is True


def test_outcome_is_not_emitted_until_exit_bar_is_available():
    report = build(
        [event()],
        [bar(1, close_hour=2, available_hour=8)],
        as_of_hours=3,
    )
    assert report.labels == ()
    assert dict(report.excluded_counts)["outcome_not_yet_matured"] == 1


def test_future_event_revision_and_future_bars_do_not_enter_snapshot():
    report = build(
        [event(), event("future", known_hours=12, sentiment=-0.8)],
        [
            bar(1, close_hour=2, available_hour=2),
            bar(2, close_hour=3, available_hour=3),
            bar(3, close_hour=4, available_hour=4),
            bar(4, close_hour=5, available_hour=5),
            bar(5, close_hour=6, available_hour=6),
            bar(6, close_hour=7, available_hour=12),
        ],
        as_of_hours=6,
    )
    assert all(label.event_id == "event-1" for label in report.labels)
    assert dict(report.excluded_counts)["event_not_yet_known"] == 1


def test_missing_entry_or_exit_bars_are_explicitly_counted():
    report = build([event()], [], as_of_hours=10)
    assert report.labels == ()
    assert dict(report.excluded_counts)["no_available_market_bars"] == 2


def test_duplicate_revisions_invalid_timestamps_and_horizons_fail_closed():
    revision = event()
    with pytest.raises(ValueError, match="duplicate event revision"):
        build([revision, revision], [], as_of_hours=10)
    with pytest.raises(ValueError, match="positive timedeltas"):
        build_point_in_time_labels(
            [revision],
            [],
            horizons={"bad": timedelta(0)},
            as_of=BASE + timedelta(hours=10),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        build_point_in_time_labels(
            [],
            [],
            horizons={"1h": timedelta(hours=1)},
            as_of=datetime(2026, 10, 1),
        )



def test_matured_labels_convert_to_training_rows_without_clipping_extreme_returns():
    report = build(
        [event()],
        [
            bar(1, close_hour=2, available_hour=2, close_price=250.0),
            bar(2, close_hour=3, available_hour=3, open_price=250.0, close_price=260.0),
            bar(3, close_hour=4, available_hour=4, open_price=260.0, close_price=270.0),
            bar(4, close_hour=5, available_hour=5, open_price=270.0, close_price=280.0),
            bar(5, close_hour=6, available_hour=6, open_price=280.0, close_price=290.0),
        ],
    )
    rows = to_event_impact_training_rows(report)
    one_hour = next(item for item in rows if item.horizon == "1h")
    assert one_hour.forward_return == pytest.approx(1.5)
    assert one_hour.event_id == "event-1:1h"
