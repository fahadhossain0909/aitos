"""Point-in-time event-study beta estimation with explicit look-ahead guards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import fsum, isfinite
from typing import Sequence


@dataclass(frozen=True, slots=True)
class EventStudyObservation:
    asset: str
    event_type: str
    horizon: str
    event_time: datetime
    outcome_available_at: datetime
    event_signal: float
    forward_return: float

    def __post_init__(self) -> None:
        if not self.asset.strip() or not self.event_type.strip() or not self.horizon.strip():
            raise ValueError("asset, event_type and horizon are required")
        for name in ("event_time", "outcome_available_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        for name in ("event_signal", "forward_return"):
            value = float(getattr(self, name))
            if not isfinite(value) or not -1.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and between -1 and 1")
            object.__setattr__(self, name, value)
        if self.outcome_available_at < self.event_time:
            raise ValueError("outcome cannot be available before the event")


@dataclass(frozen=True, slots=True)
class EventBetaEstimate:
    asset: str
    event_type: str
    horizon: str
    beta: float
    sample_count: int
    sample_sufficiency: float
    as_of: datetime
    status: str


def estimate_event_beta(
    observations: Sequence[EventStudyObservation],
    *,
    asset: str,
    event_type: str,
    horizon: str,
    as_of: datetime,
    min_samples: int = 30,
    shrinkage_strength: float = 20.0,
) -> EventBetaEstimate:
    """Estimate beta only from outcomes fully observable at the as-of time.

    The slope is shrunk toward zero and bounded to [-2, 2]. The returned
    sample_sufficiency is a coverage diagnostic, not a calibrated probability.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if min_samples < 2 or not isfinite(float(shrinkage_strength)) or shrinkage_strength < 0:
        raise ValueError("invalid event-study estimation policy")

    cutoff = as_of.astimezone(timezone.utc)
    eligible = [
        item
        for item in observations
        if item.asset.upper() == asset.upper()
        and item.event_type == event_type
        and item.horizon == horizon
        and item.event_time.astimezone(timezone.utc) < cutoff
        and item.outcome_available_at.astimezone(timezone.utc) <= cutoff
    ]
    count = len(eligible)
    sufficiency = min(1.0, count / float(min_samples * 2))
    if count < min_samples:
        return EventBetaEstimate(
            asset.upper(),
            event_type,
            horizon,
            0.0,
            count,
            sufficiency,
            cutoff,
            "insufficient_history",
        )

    signals = [item.event_signal for item in eligible]
    returns = [item.forward_return for item in eligible]
    mean_signal = fsum(signals) / count
    mean_return = fsum(returns) / count
    variance = fsum((value - mean_signal) ** 2 for value in signals)
    if variance <= 1e-12:
        return EventBetaEstimate(
            asset.upper(),
            event_type,
            horizon,
            0.0,
            count,
            sufficiency,
            cutoff,
            "no_signal_variance",
        )

    covariance = fsum(
        (signal - mean_signal) * (forward_return - mean_return)
        for signal, forward_return in zip(signals, returns)
    )
    raw_beta = covariance / variance
    shrinkage = count / (count + float(shrinkage_strength))
    beta = max(-2.0, min(2.0, raw_beta * shrinkage))
    return EventBetaEstimate(
        asset.upper(),
        event_type,
        horizon,
        beta,
        count,
        sufficiency,
        cutoff,
        "estimated",
    )


__all__ = [
    "EventStudyObservation",
    "EventBetaEstimate",
    "estimate_event_beta",
]
