from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.verification import verify_observations

from tests.test_world_intelligence import make_event


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def test_same_publisher_group_does_not_count_as_independent():
    first = make_event()
    second = make_event(
        event_id="evt-2",
        source=first.source.__class__(
            source_id="syndicated-copy",
            source_kind="news",
            uri="https://copy.example.test/story/1",
            published_at=first.source.published_at,
            first_seen_at=NOW,
            reliability=0.9,
        ),
    )
    result = verify_observations(
        [first, second],
        publisher_groups={"sec": "publisher-a", "syndicated-copy": "publisher-a"},
        now=NOW,
    )
    assert result.status == "single_source"
    assert not result.verified
    assert result.independent_source_count == 1


def test_independent_publisher_groups_confirm_observation():
    first = make_event()
    second = make_event(
        event_id="evt-2",
        source=first.source.__class__(
            source_id="independent-news",
            source_kind="news",
            uri="https://independent.example.test/story/1",
            published_at=first.source.published_at,
            first_seen_at=NOW,
            reliability=0.9,
        ),
    )
    result = verify_observations(
        [first, second],
        publisher_groups={"sec": "official", "independent-news": "publisher-b"},
        now=NOW,
    )
    assert result.status == "multi_source_confirmed"
    assert result.verified
    assert result.independent_source_count == 2


def test_fresh_high_reliability_primary_source_is_sufficient():
    result = verify_observations(
        [make_event()],
        publisher_groups={"sec": "official"},
        now=NOW,
        primary_source_reliability=0.9,
    )
    assert result.status == "primary_source"
    assert result.verified


def test_stale_and_unmapped_sources_are_not_confirmation():
    event = make_event(
        first_seen_at=NOW - timedelta(hours=3),
        source=make_event().source.__class__(
            source_id="unknown",
            source_kind="news",
            uri="https://unknown.example.test/story",
            published_at=NOW - timedelta(hours=4),
            first_seen_at=NOW - timedelta(hours=3),
            reliability=1.0,
        ),
    )
    result = verify_observations(
        [event], publisher_groups={"unknown": "publisher"}, now=NOW
    )
    assert result.status == "unverified"


def test_disputed_or_retracted_observations_fail_closed():
    disputed = verify_observations(
        [make_event(verification="disputed")],
        publisher_groups={"sec": "official"},
        now=NOW,
    )
    retracted = verify_observations(
        [make_event(verification="retracted")],
        publisher_groups={"sec": "official"},
        now=NOW,
    )
    assert disputed.status == "disputed"
    assert retracted.status == "retracted"
    assert not disputed.verified
    assert not retracted.verified
