"""Read-only, point-in-time world-intelligence context for scanner shadow use."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Protocol

from aitos.world_intelligence.event_study import EventBetaEstimate
from aitos.world_intelligence.snapshot import (
    AssetIntelligenceSnapshot,
    build_asset_intelligence_snapshot,
)
from aitos.world_intelligence.storage import ClickHouseIntelligenceStore


class AssetSnapshotProvider(Protocol):
    async def snapshot_for_asset(
        self, symbol: str, *, as_of: datetime
    ) -> AssetIntelligenceSnapshot | None: ...


def _base_asset(symbol: str) -> str:
    normalized = symbol.strip().upper()
    for quote in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB", "EUR"):
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)]
    return normalized


def snapshot_to_dict(snapshot: AssetIntelligenceSnapshot) -> dict[str, Any]:
    """Serialize only compact evidence; never turn it into a trading instruction."""
    return {
        "schema_version": 1,
        "asset": snapshot.asset,
        "as_of": snapshot.as_of.isoformat(),
        "event_count": snapshot.event_count,
        "verified_event_count": snapshot.verified_event_count,
        "unverified_event_count": snapshot.unverified_event_count,
        "attention_score": snapshot.attention_score,
        "horizon_impacts": [asdict(item) for item in snapshot.horizon_impacts],
        "risk_assessment": asdict(snapshot.risk_assessment),
        "policy": "shadow_only_no_execution_or_sizing_authority",
    }


class ClickHouseAssetSnapshotProvider:
    """Build a cached-by-caller snapshot from persisted point-in-time events."""

    def __init__(
        self,
        store: ClickHouseIntelligenceStore,
        *,
        lookback_hours: float = 72.0,
        max_events: int = 5000,
        beta_estimates: dict[tuple[str, str], EventBetaEstimate] | None = None,
    ):
        if not 0 < lookback_hours <= 24 * 90:
            raise ValueError("lookback_hours must be between 0 and 2160")
        if not 1 <= max_events <= 20_000:
            raise ValueError("max_events must be between 1 and 20000")
        self.store = store
        self.lookback_hours = float(lookback_hours)
        self.max_events = max_events
        self.beta_estimates = dict(beta_estimates or {})

    async def snapshot_for_asset(
        self, symbol: str, *, as_of: datetime
    ) -> AssetIntelligenceSnapshot:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        cutoff = as_of.astimezone(timezone.utc)
        normalized = symbol.strip().upper()
        base = _base_asset(normalized)
        events = await self.store.load_recent_events(
            as_of=cutoff,
            lookback_hours=self.lookback_hours,
            limit=self.max_events,
        )
        matched = [
            event for event in events
            if normalized in event.entities or base in event.entities
        ]
        return build_asset_intelligence_snapshot(
            matched,
            asset=normalized,
            as_of=cutoff,
            beta_estimates=self.beta_estimates,
        )


__all__ = [
    "AssetSnapshotProvider",
    "ClickHouseAssetSnapshotProvider",
    "snapshot_to_dict",
]
