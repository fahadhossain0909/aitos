"""Point-in-time event replay primitives for CFEI research validation.

This module builds outcome labels only from prices observed at or after the
predeclared horizon. It does not emit trading signals or execute orders.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Iterable, Sequence

from .contracts import CapitalEvent


@dataclass(frozen=True, slots=True)
class PriceObservation:
    instrument: str
    observed_at: datetime
    price: float

    def __post_init__(self) -> None:
        if not self.instrument.strip() or self.observed_at.tzinfo is None:
            raise ValueError("price instrument and timezone-aware timestamp required")
        if not isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be positive and finite")
        object.__setattr__(
            self, "observed_at", self.observed_at.astimezone(timezone.utc)
        )


@dataclass(frozen=True, slots=True)
class ReplayRow:
    event_id: str
    instrument: str
    event_time: datetime
    event_type: str
    direction: int
    flow_usd: float
    forward_returns: dict[int, float]


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    horizon_seconds: int
    sample_count: int
    hit_rate: float | None
    mean_return: float | None
    median_return: float | None
    net_hit_rate: float | None = None
    net_mean_return: float | None = None
    total_cost_bps: float = 0.0


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _direction(event: CapitalEvent) -> int:
    metadata = dict(event.metadata)
    explicit = metadata.get("flow_sign")
    if explicit in (-1, 1):
        return int(explicit)
    if metadata.get("flow_direction") == "inflow":
        return 1
    if metadata.get("flow_direction") == "outflow":
        return -1
    return 0


def build_point_in_time_replay(
    events: Iterable[CapitalEvent],
    prices: Iterable[PriceObservation],
    *,
    horizons_seconds: Sequence[int] = (300, 900, 3600),
    max_price_age_seconds: float = 30.0,
) -> list[ReplayRow]:
    """Create event rows with forward returns and strict temporal safeguards."""
    horizons = tuple(sorted(set(int(value) for value in horizons_seconds)))
    if not horizons or any(value <= 0 for value in horizons):
        raise ValueError("replay horizons must be positive seconds")
    if not isfinite(max_price_age_seconds) or max_price_age_seconds <= 0:
        raise ValueError("max price age must be positive and finite")

    price_index: dict[str, list[tuple[datetime, float]]] = {}
    for observation in prices:
        price_index.setdefault(observation.instrument, []).append(
            (observation.observed_at, observation.price)
        )
    for rows in price_index.values():
        rows.sort(key=lambda item: item[0])

    output: list[ReplayRow] = []
    for event in sorted(events, key=lambda item: (item.event_time, item.event_id)):
        if event.finality == "reverted" or event.usd_value is None:
            continue
        if event.event_time > event.observed_time:
            # A source cannot legitimately observe an event before it occurred.
            continue
        direction = _direction(event)
        if direction == 0:
            # Unknown flow direction must not be assigned a bullish/bearish sign.
            continue
        observations = price_index.get(event.instrument, [])
        if not observations:
            continue
        times = [item[0] for item in observations]
        event_index = bisect_left(times, event.event_time)
        if event_index >= len(observations):
            continue
        event_time, entry_price = observations[event_index]
        if abs((event_time - event.event_time).total_seconds()) > max_price_age_seconds:
            continue

        forward: dict[int, float] = {}
        for horizon in horizons:
            target = event.event_time + timedelta(seconds=horizon)
            target_index = bisect_left(times, target)
            if target_index >= len(observations):
                continue
            target_time, target_price = observations[target_index]
            if (target_time - target).total_seconds() > max_price_age_seconds:
                continue
            raw_return = target_price / entry_price - 1.0
            forward[horizon] = direction * raw_return
        if forward:
            output.append(
                ReplayRow(
                    event_id=event.event_id,
                    instrument=event.instrument,
                    event_time=event.event_time.astimezone(timezone.utc),
                    event_type=event.event_type,
                    direction=direction,
                    flow_usd=float(event.usd_value),
                    forward_returns=forward,
                )
            )
    return output


def summarize_replay(
    rows: Iterable[ReplayRow],
    *,
    horizon_seconds: int,
    fee_bps_per_side: float = 0.0,
    spread_bps_round_trip: float = 0.0,
    slippage_bps_per_side: float = 0.0,
    funding_bps_per_8h: float = 0.0,
) -> ReplaySummary:
    """Report gross and cost-adjusted outcomes without significance claims.

    Fees and slippage are charged on entry and exit. Spread is a round-trip
    crossing cost. Funding is a non-negative cost accrued pro-rata by horizon;
    use a richer venue-specific ledger for realized signed funding.
    """
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    costs = (
        fee_bps_per_side,
        spread_bps_round_trip,
        slippage_bps_per_side,
        funding_bps_per_8h,
    )
    if any(not isfinite(value) or value < 0 or value > 1000 for value in costs):
        raise ValueError("replay costs must be finite and in [0, 1000] bps")
    total_cost_bps = (
        2.0 * fee_bps_per_side
        + spread_bps_round_trip
        + 2.0 * slippage_bps_per_side
        + funding_bps_per_8h * horizon_seconds / 28_800.0
    )
    returns = [
        row.forward_returns[horizon_seconds]
        for row in rows
        if horizon_seconds in row.forward_returns
    ]
    if not returns:
        return ReplaySummary(
            horizon_seconds, 0, None, None, None, None, None, total_cost_bps
        )
    cost_return = total_cost_bps / 10_000.0
    net_returns = [value - cost_return for value in returns]
    return ReplaySummary(
        horizon_seconds=horizon_seconds,
        sample_count=len(returns),
        hit_rate=sum(value > 0 for value in returns) / len(returns),
        mean_return=sum(returns) / len(returns),
        median_return=_median(returns),
        net_hit_rate=sum(value > 0 for value in net_returns) / len(net_returns),
        net_mean_return=sum(net_returns) / len(net_returns),
        total_cost_bps=total_cost_bps,
    )


def chronological_split(
    rows: Iterable[ReplayRow],
    *,
    test_fraction: float = 0.2,
    embargo_seconds: int = 0,
) -> tuple[list[ReplayRow], list[ReplayRow]]:
    """Chronological split with label-horizon purging and optional embargo.

    A training event is removed when its longest forward label can reach the
    first test event. This prevents overlapping outcome windows from leaking
    test-period price information into the training labels.
    """
    if not 0.05 <= test_fraction <= 0.5:
        raise ValueError("test_fraction must be in [0.05, 0.5]")
    if not isinstance(embargo_seconds, int) or embargo_seconds < 0:
        raise ValueError("embargo_seconds must be a non-negative integer")
    ordered = sorted(rows, key=lambda row: (row.event_time, row.event_id))
    if len(ordered) < 2:
        return ordered, []
    split_at = max(1, min(len(ordered) - 1, int(len(ordered) * (1 - test_fraction))))
    test = ordered[split_at:]
    test_start = test[0].event_time
    train = [
        row
        for row in ordered[:split_at]
        if row.forward_returns
        and row.event_time
        + timedelta(seconds=max(row.forward_returns))
        + timedelta(seconds=embargo_seconds)
        < test_start
    ]
    return train, test
