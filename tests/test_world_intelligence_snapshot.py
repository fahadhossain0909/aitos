from datetime import datetime, timedelta, timezone

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.event_study import EventBetaEstimate
from aitos.world_intelligence.snapshot import build_asset_intelligence_snapshot


AS_OF = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def make_event(event_id="evt", verification="primary_source", severity=0.9):
    source = SourceEvidence(
        source_id="official",
        source_kind="official",
        uri="https://example.test/event",
        published_at=AS_OF - timedelta(minutes=1),
        first_seen_at=AS_OF,
        reliability=1.0,
        is_primary=True,
    )
    return IntelligenceEvent(
        event_id=event_id,
        event_type="regulatory",
        title="Official event",
        entities=("BTC",),
        published_at=AS_OF - timedelta(minutes=1),
        first_seen_at=AS_OF,
        source=source,
        sentiment=-0.8,
        severity=severity,
        novelty=0.8,
        confidence=0.95,
        verification=verification,
    )


def test_snapshot_uses_only_point_in_time_events_and_beta_estimates():
    event = make_event()
    future_event = make_event("future")
    future_source = SourceEvidence(
        source_id="future-source",
        source_kind="news",
        uri="https://future.example.test/event",
        published_at=AS_OF + timedelta(minutes=1),
        first_seen_at=AS_OF + timedelta(minutes=1),
        reliability=0.9,
    )
    future_event = IntelligenceEvent(
        event_id=future_event.event_id,
        event_type=future_event.event_type,
        title=future_event.title,
        entities=future_event.entities,
        published_at=AS_OF + timedelta(minutes=1),
        first_seen_at=AS_OF + timedelta(minutes=1),
        source=future_source,
        sentiment=future_event.sentiment,
        severity=future_event.severity,
        novelty=future_event.novelty,
        confidence=future_event.confidence,
        verification=future_event.verification,
    )
    beta = EventBetaEstimate(
        asset="BTCUSDT",
        event_type="regulatory",
        horizon="1h",
        beta=0.8,
        sample_count=100,
        sample_sufficiency=1.0,
        as_of=AS_OF - timedelta(days=1),
        status="estimated",
    )
    snapshot = build_asset_intelligence_snapshot(
        [event, future_event],
        asset="BTCUSDT",
        as_of=AS_OF,
        beta_estimates={("regulatory", "1h"): beta},
    )
    assert snapshot.event_count == 1
    assert snapshot.verified_event_count == 1
    assert snapshot.horizon_impacts[0].signed_impact < 0


def test_snapshot_ignores_beta_estimates_created_after_as_of():
    event = make_event()
    future_beta = EventBetaEstimate(
        asset="BTCUSDT",
        event_type="regulatory",
        horizon="1h",
        beta=1.0,
        sample_count=100,
        sample_sufficiency=1.0,
        as_of=AS_OF + timedelta(seconds=1),
        status="estimated",
    )
    snapshot = build_asset_intelligence_snapshot(
        [event],
        asset="BTCUSDT",
        as_of=AS_OF,
        beta_estimates={("regulatory", "1h"): future_beta},
    )
    assert snapshot.horizon_impacts[0].signed_impact == 0.0
    assert snapshot.horizon_impacts[0].evidence_count == 1


def test_snapshot_unverified_events_are_attention_only():
    event = make_event(verification="unverified")
    snapshot = build_asset_intelligence_snapshot([event], asset="BTCUSDT", as_of=AS_OF)
    assert snapshot.event_count == 1
    assert snapshot.verified_event_count == 0
    assert snapshot.horizon_impacts[0].signed_impact == 0.0
    assert snapshot.risk_assessment.status == "unverified"


def test_snapshot_emits_advisory_critical_event_review():
    event = make_event(severity=0.95)
    snapshot = build_asset_intelligence_snapshot(
        [event], asset="BTCUSDT", as_of=AS_OF
    )
    assert snapshot.risk_assessment.recommend_new_entry_block
    assert snapshot.risk_assessment.exposure_multiplier == 0.5


def test_snapshot_attention_decays_with_event_age():
    fresh = make_event()
    old_time = AS_OF - timedelta(hours=12)
    old_source = SourceEvidence(
        source_id="official",
        source_kind="official",
        uri="https://example.test/old-event",
        published_at=old_time - timedelta(minutes=1),
        first_seen_at=old_time,
        reliability=1.0,
        is_primary=True,
    )
    old_event = IntelligenceEvent(
        event_id="old",
        event_type="regulatory",
        title="Old official event",
        entities=("BTC",),
        published_at=old_time - timedelta(minutes=1),
        first_seen_at=old_time,
        source=old_source,
        sentiment=-0.8,
        severity=0.9,
        novelty=0.8,
        confidence=0.95,
        verification="primary_source",
    )
    fresh_snapshot = build_asset_intelligence_snapshot(
        [fresh], asset="BTCUSDT", as_of=AS_OF
    )
    old_snapshot = build_asset_intelligence_snapshot(
        [old_event], asset="BTCUSDT", as_of=AS_OF
    )
    assert old_snapshot.attention_score < fresh_snapshot.attention_score
