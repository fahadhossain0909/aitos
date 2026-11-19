"""Point-in-time event revisions and realized forward-return labels.

Event features are frozen to the first version known when the event entered
the system. Market bars use their first observed revision for labels. This
prevents later article corrections or backfilled market-data revisions from
silently rewriting historical training rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class EventRevision:
    event_id: str
    revision_id: str
    asset: str
    event_type: str
    published_at: datetime
    known_at: datetime
    sentiment: float
    severity: float
    novelty: float
    confidence: float
    source_reliability: float
    regime: str = "unknown"
    beta: float = 0.0

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.event_id,
                self.revision_id,
                self.asset,
                self.event_type,
                self.regime,
            )
        ):
            raise ValueError("event and revision identity fields are required")
        object.__setattr__(self, "asset", self.asset.upper())
        object.__setattr__(
            self, "published_at", _utc(self.published_at, "published_at")
        )
        object.__setattr__(self, "known_at", _utc(self.known_at, "known_at"))
        if self.known_at < self.published_at:
            raise ValueError("known_at cannot precede published_at")
        for name, lower, upper in (
            ("sentiment", -1.0, 1.0),
            ("severity", 0.0, 1.0),
            ("novelty", 0.0, 1.0),
            ("confidence", 0.0, 1.0),
            ("source_reliability", 0.0, 1.0),
            ("beta", -2.0, 2.0),
        ):
            value = float(getattr(self, name))
            if not isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{name} must be finite and within [{lower}, {upper}]")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class MarketBarRevision:
    asset: str
    revision_id: str
    open_time: datetime
    close_time: datetime
    available_at: datetime
    open_price: float
    close_price: float

    def __post_init__(self) -> None:
        if not self.asset.strip() or not self.revision_id.strip():
            raise ValueError("asset and revision_id are required")
        object.__setattr__(self, "asset", self.asset.upper())
        for name in ("open_time", "close_time", "available_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.close_time <= self.open_time:
            raise ValueError("bar close_time must be after open_time")
        if self.available_at < self.close_time:
            raise ValueError("bar cannot be available before it closes")
        for name in ("open_price", "close_price"):
            value = float(getattr(self, name))
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class RealizedEventLabel:
    event_id: str
    asset: str
    event_type: str
    regime: str
    horizon: str
    event_time: datetime
    entry_time: datetime
    exit_time: datetime
    outcome_available_at: datetime
    entry_price: float
    exit_price: float
    forward_return: float
    direction_up: bool
    sentiment: float
    severity: float
    novelty: float
    confidence: float
    source_reliability: float
    beta: float


@dataclass(frozen=True, slots=True)
class PointInTimeLabelReport:
    labels: tuple[RealizedEventLabel, ...]
    excluded_counts: tuple[tuple[str, int], ...]
    as_of: datetime


def build_point_in_time_labels(
    event_revisions: Sequence[EventRevision],
    market_bars: Sequence[MarketBarRevision],
    *,
    horizons: Mapping[str, timedelta],
    as_of: datetime,
) -> PointInTimeLabelReport:
    """Build labels from first-known event/bar revisions only.

    Decision time is when AITOS first knew the event, not the publisher's
    backdated timestamp. Entry is the first bar opening at or after decision
    time; exit is the first bar closing at or after the requested horizon.
    Both selected bar versions must have been available by as_of.
    """
    cutoff = _utc(as_of, "as_of")
    if not horizons:
        raise ValueError("at least one forward horizon is required")
    normalized_horizons: dict[str, timedelta] = {}
    for name, duration in horizons.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("horizon names must be non-empty strings")
        if not isinstance(duration, timedelta) or duration <= timedelta(0):
            raise ValueError("horizon durations must be positive timedeltas")
        normalized_horizons[name] = duration

    seen_event_revisions: set[tuple[str, str]] = set()
    first_events: dict[str, EventRevision] = {}
    excluded = Counter()
    for revision in event_revisions:
        key = (revision.event_id, revision.revision_id)
        if key in seen_event_revisions:
            raise ValueError("duplicate event revision identity")
        seen_event_revisions.add(key)
        if revision.known_at > cutoff:
            excluded["event_not_yet_known"] += 1
            continue
        current = first_events.get(revision.event_id)
        if current is None or (revision.known_at, revision.revision_id) < (
            current.known_at,
            current.revision_id,
        ):
            first_events[revision.event_id] = revision

    seen_bar_revisions: set[tuple[str, datetime, str]] = set()
    first_bars: dict[tuple[str, datetime], MarketBarRevision] = {}
    for bar in market_bars:
        key = (bar.asset, bar.open_time, bar.revision_id)
        if key in seen_bar_revisions:
            raise ValueError("duplicate market bar revision identity")
        seen_bar_revisions.add(key)
        current_key = (bar.asset, bar.open_time)
        current = first_bars.get(current_key)
        if current is None or (bar.available_at, bar.revision_id) < (
            current.available_at,
            current.revision_id,
        ):
            first_bars[current_key] = bar

    bars_by_asset: dict[str, list[MarketBarRevision]] = defaultdict(list)
    for bar in first_bars.values():
        if bar.available_at <= cutoff:
            bars_by_asset[bar.asset].append(bar)
        else:
            excluded["bar_not_yet_available"] += 1
    for asset_bars in bars_by_asset.values():
        asset_bars.sort(key=lambda item: (item.open_time, item.close_time))

    labels: list[RealizedEventLabel] = []
    for event in sorted(
        first_events.values(), key=lambda item: (item.known_at, item.event_id)
    ):
        asset_bars = bars_by_asset.get(event.asset, [])
        if not asset_bars:
            excluded["no_available_market_bars"] += len(normalized_horizons)
            continue
        for horizon_name, duration in normalized_horizons.items():
            target_time = event.known_at + duration
            entry = next(
                (bar for bar in asset_bars if bar.open_time >= event.known_at),
                None,
            )
            if entry is None:
                excluded["no_entry_bar"] += 1
                continue
            if entry.open_time >= target_time:
                excluded["entry_after_horizon"] += 1
                continue
            exit_bar = next(
                (
                    bar
                    for bar in asset_bars
                    if bar.close_time >= target_time
                    and bar.open_time >= entry.open_time
                ),
                None,
            )
            if exit_bar is None:
                excluded["no_exit_bar"] += 1
                continue
            if exit_bar.close_time <= entry.open_time:
                excluded["invalid_bar_sequence"] += 1
                continue
            outcome_available_at = max(entry.available_at, exit_bar.available_at)
            if outcome_available_at > cutoff:
                excluded["outcome_not_yet_matured"] += 1
                continue
            forward_return = exit_bar.close_price / entry.open_price - 1.0
            labels.append(
                RealizedEventLabel(
                    event_id=event.event_id,
                    asset=event.asset,
                    event_type=event.event_type,
                    regime=event.regime,
                    horizon=horizon_name,
                    event_time=event.known_at,
                    entry_time=entry.open_time,
                    exit_time=exit_bar.close_time,
                    outcome_available_at=outcome_available_at,
                    entry_price=entry.open_price,
                    exit_price=exit_bar.close_price,
                    forward_return=forward_return,
                    direction_up=forward_return > 0.0,
                    sentiment=event.sentiment,
                    severity=event.severity,
                    novelty=event.novelty,
                    confidence=event.confidence,
                    source_reliability=event.source_reliability,
                    beta=event.beta,
                )
            )
    return PointInTimeLabelReport(
        labels=tuple(labels),
        excluded_counts=tuple(sorted(excluded.items())),
        as_of=cutoff,
    )


__all__ = [
    "EventRevision",
    "MarketBarRevision",
    "PointInTimeLabelReport",
    "RealizedEventLabel",
    "build_point_in_time_labels",
]
