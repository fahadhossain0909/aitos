from datetime import datetime, timedelta, timezone

import pytest

from aitos.world_intelligence.context import (
    ClickHouseAssetSnapshotProvider,
    snapshot_to_dict,
)
from aitos.world_intelligence.contracts import IntelligenceEvent, SourceEvidence
from aitos.world_intelligence.snapshot import build_asset_intelligence_snapshot


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self, events):
        self.events = events
        self.request = None

    async def load_recent_events(self, **kwargs):
        self.request = kwargs
        return self.events


def event(event_id, entities, first_seen):
    source = SourceEvidence(
        source_id="sec",
        source_kind="official_filing",
        uri=f"https://sec.example/{event_id}",
        published_at=first_seen - timedelta(minutes=1),
        first_seen_at=first_seen,
        reliability=0.99,
        is_primary=True,
    )
    return IntelligenceEvent(
        event_id=event_id,
        event_type="regulatory",
        title="Bitcoin exchange filing",
        entities=entities,
        published_at=source.published_at,
        first_seen_at=first_seen,
        source=source,
        sentiment=-0.5,
        severity=0.9,
        novelty=0.8,
        confidence=0.9,
        verification="primary_source",
    )


@pytest.mark.asyncio
async def test_asset_snapshot_provider_filters_by_base_asset_and_keeps_beta_neutral():
    store = Store(
        [
            event("btc", ("BTC",), NOW - timedelta(minutes=1)),
            event("eth", ("ETH",), NOW - timedelta(minutes=1)),
        ]
    )
    provider = ClickHouseAssetSnapshotProvider(store)
    snapshot = await provider.snapshot_for_asset("BTCUSDT", as_of=NOW)
    assert snapshot.asset == "BTCUSDT"
    assert snapshot.event_count == 1
    assert snapshot.verified_event_count == 1
    assert all(item.signed_impact == 0.0 for item in snapshot.horizon_impacts)
    assert store.request["as_of"] == NOW


@pytest.mark.asyncio
async def test_snapshot_provider_rejects_naive_as_of():
    provider = ClickHouseAssetSnapshotProvider(Store([]))
    with pytest.raises(ValueError, match="timezone-aware"):
        await provider.snapshot_for_asset("BTCUSDT", as_of=datetime(2026, 11, 13))


def test_snapshot_serialization_declares_shadow_only_policy():
    snapshot = build_asset_intelligence_snapshot([], asset="BTCUSDT", as_of=NOW)
    payload = snapshot_to_dict(snapshot)
    assert payload["policy"] == "shadow_only_no_execution_or_sizing_authority"
    assert payload["schema_version"] == 1
