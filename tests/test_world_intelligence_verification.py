from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.verification import verify_observations


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def make_event(
    event_id="evt-1",
    source_id="source-a",
    verification="unverified",
    first_seen_at=NOW,
    reliability=0.9,
    is_primary=False,
):
    published_at = min(first_seen_at, NOW) - timedelta(minutes=1)
    source = SourceEvidence(
        source_id=source_id,
        source_kind="news",
        uri=f"https://{source_id}.example.test/story/1",
        published_at=published_at,
        first_seen_at=first_seen_at,
        reliability=reliability,
        is_primary=is_primary,
    )
    return IntelligenceEvent(
        event_id=event_id,
        event_type="news",
        title="Example event",
        entities=("BTC",),
        published_at=published_at,
        first_seen_at=first_seen_at,
        source=source,
        sentiment=0.0,
        severity=0.5,
        novelty=0.5,
        confidence=0.5,
        verification=verification,
    )


def test_same_publisher_group_does_not_count_as_independent():
    result = verify_observations(
        [
            make_event(source_id="feed-a"),
            make_event(event_id="evt-2", source_id="feed-b"),
        ],
        publisher_groups={"feed-a": "publisher-a", "feed-b": "publisher-a"},
        now=NOW,
    )
    assert result.status == "single_source"
    assert not result.verified
    assert result.independent_source_count == 1


def test_independent_publisher_groups_confirm_observation():
    result = verify_observations(
        [
            make_event(source_id="official"),
            make_event(event_id="evt-2", source_id="independent-news"),
        ],
        publisher_groups={
            "official": "publisher-a",
            "independent-news": "publisher-b",
        },
        now=NOW,
    )
    assert result.status == "multi_source_confirmed"
    assert result.verified
    assert result.independent_source_count == 2


def test_fresh_high_reliability_primary_source_is_sufficient():
    result = verify_observations(
        [make_event(source_id="official", is_primary=True)],
        publisher_groups={"official": "official-publisher"},
        now=NOW,
        primary_source_reliability=0.9,
    )
    assert result.status == "primary_source"
    assert result.verified


def test_stale_and_unmapped_sources_are_not_confirmation():
    event = make_event(
        source_id="unknown",
        first_seen_at=NOW - timedelta(hours=3),
    )
    result = verify_observations(
        [event], publisher_groups={"unknown": "publisher"}, now=NOW
    )
    assert result.status == "unverified"


def test_disputed_or_retracted_observations_fail_closed():
    disputed = verify_observations(
        [make_event(verification="disputed")],
        publisher_groups={"source-a": "official"},
        now=NOW,
    )
    retracted = verify_observations(
        [make_event(verification="retracted")],
        publisher_groups={"source-a": "official"},
        now=NOW,
    )
    assert disputed.status == "disputed"
    assert retracted.status == "retracted"
    assert not disputed.verified
    assert not retracted.verified
