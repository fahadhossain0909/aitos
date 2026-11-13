from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.source_quality import (
    SourceOutcome,
    assess_source_batch,
    estimate_source_reliability,
)


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def make_event(index, title="Repeated headline", source_id="feed"):
    source = SourceEvidence(
        source_id=source_id,
        source_kind="news",
        uri=f"https://example.test/{index}",
        published_at=NOW - timedelta(minutes=1),
        first_seen_at=NOW - timedelta(seconds=1),
        reliability=0.5,
        original_story_id=f"story-{index}",
    )
    return IntelligenceEvent(
        event_id=str(index),
        event_type="news",
        title=title,
        entities=("BTC",),
        published_at=source.published_at,
        first_seen_at=source.first_seen_at,
        source=source,
        sentiment=0.0,
        severity=0.1,
        novelty=0.5,
        confidence=0.2,
    )


def test_source_quality_flags_repetitive_bursts_without_asserting_bot_identity():
    events = [make_event(index) for index in range(8)]
    reports = assess_source_batch(
        events,
        as_of=NOW,
        window_seconds=60,
        burst_threshold_per_minute=5,
        duplicate_fraction_threshold=0.6,
    )
    assert reports[0].suspected_automation
    assert "high_exact_or_story_duplicate_fraction" in reports[0].reasons


def test_source_quality_excludes_future_observations():
    future = make_event(9)
    future_source = SourceEvidence(
        source_id="feed",
        source_kind="news",
        uri="https://example.test/future",
        published_at=NOW + timedelta(minutes=1),
        first_seen_at=NOW + timedelta(minutes=1),
        reliability=0.5,
    )
    future_event = IntelligenceEvent(
        event_id="future",
        event_type="news",
        title="Future story",
        entities=("BTC",),
        published_at=future_source.published_at,
        first_seen_at=future_source.first_seen_at,
        source=future_source,
        sentiment=0.0,
        severity=0.1,
        novelty=0.5,
        confidence=0.2,
    )
    reports = assess_source_batch([future, future_event], as_of=NOW)
    assert reports[0].observation_count == 1


def test_source_reliability_uses_only_labels_available_as_of():
    outcomes = [
        SourceOutcome("feed", NOW - timedelta(days=1), True),
        SourceOutcome("feed", NOW - timedelta(hours=1), False),
        SourceOutcome("feed", NOW + timedelta(hours=1), True),
    ]
    estimate = estimate_source_reliability(
        outcomes, source_id="feed", as_of=NOW, min_samples=2
    )
    assert estimate.sample_count == 2
    assert estimate.confirmed_count == 1
    assert estimate.status == "calibrated"
    assert 0.0 < estimate.posterior_mean < 1.0
