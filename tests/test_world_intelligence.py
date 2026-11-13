from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.governor import IntelligenceRiskGovernor
from aitos.world_intelligence.impact import estimate_asset_impact


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def make_event(**overrides):
    source = SourceEvidence(
        source_id="sec",
        source_kind="official",
        uri="https://example.test/filing/1",
        published_at=NOW - timedelta(minutes=1),
        first_seen_at=NOW,
        reliability=1.0,
        is_primary=True,
    )
    values = dict(
        event_id="evt-1",
        event_type="regulatory",
        title="Official filing",
        entities=("BTC", "btc", "ETH"),
        published_at=NOW - timedelta(minutes=1),
        first_seen_at=NOW,
        source=source,
        sentiment=-0.8,
        severity=0.9,
        novelty=0.8,
        confidence=0.95,
        verification="primary_source",
        impact_horizon="intraday",
    )
    values.update(overrides)
    return IntelligenceEvent(**values)


def test_event_normalizes_entities_and_serializes_timestamps():
    event = make_event()
    assert event.entities == ("BTC", "ETH")
    assert event.to_dict()["published_at"].endswith("+00:00")
    assert len(event.deduplication_key) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("sentiment", 1.1),
        ("severity", float("nan")),
        ("confidence", -0.1),
    ],
)
def test_event_rejects_invalid_scores(field, value):
    with pytest.raises(ValueError):
        make_event(**{field: value})


def test_event_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        make_event(published_at=datetime(2026, 11, 13, 12, 0))


def test_unverified_event_has_no_directional_impact():
    impact = estimate_asset_impact(
        make_event(verification="unverified"),
        asset="BTCUSDT",
        horizon_hours=1,
        beta=1.0,
        now=NOW,
    )
    assert impact.signed_impact == 0.0
    assert impact.confidence == 0.0


def test_missing_beta_is_neutral_not_guessed():
    impact = estimate_asset_impact(
        make_event(), asset="BTCUSDT", horizon_hours=1, now=NOW
    )
    assert impact.signed_impact == 0.0


def test_impact_is_bounded_and_decays():
    event = make_event()
    recent = estimate_asset_impact(
        event, asset="BTCUSDT", horizon_hours=1, beta=1.0, now=NOW
    )
    old = estimate_asset_impact(
        event,
        asset="BTCUSDT",
        horizon_hours=1,
        beta=1.0,
        now=NOW + timedelta(hours=12),
    )
    assert -1.0 <= recent.signed_impact <= 1.0
    assert abs(old.signed_impact) < abs(recent.signed_impact)


def test_governor_unverified_and_stale_are_non_authoritative():
    governor = IntelligenceRiskGovernor(max_age_seconds=60)
    unverified = governor.assess(
        available=True,
        observed_at=NOW,
        verified=False,
        severity=1.0,
        now=NOW,
    )
    stale = governor.assess(
        available=True,
        observed_at=NOW - timedelta(minutes=5),
        verified=True,
        severity=1.0,
        now=NOW,
    )
    assert not unverified.recommend_new_entry_block
    assert not stale.recommend_new_entry_block
    assert unverified.exposure_multiplier == 1.0


def test_governor_critical_verified_event_recommends_conservative_review():
    result = IntelligenceRiskGovernor().assess(
        available=True,
        observed_at=NOW,
        verified=True,
        severity=0.95,
        now=NOW,
    )
    assert result.recommend_new_entry_block
    assert result.exposure_multiplier == 0.5


def test_single_source_event_has_no_directional_impact():
    impact = estimate_asset_impact(
        make_event(verification="single_source"),
        asset="BTCUSDT",
        horizon_hours=1,
        beta=1.0,
        now=NOW,
    )
    assert impact.signed_impact == 0.0
    assert impact.confidence == 0.0
