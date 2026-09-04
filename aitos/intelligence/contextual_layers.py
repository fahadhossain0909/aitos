"""Unified contextual feature contracts for AITOS.

The layer deliberately separates information sources from trading decisions.
Each source produces measurable evidence; the decision engine decides whether
that evidence is relevant, reliable, contradictory, or actionable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Mapping


@dataclass(frozen=True)
class PositioningContext:
    """Derivatives positioning; optional outside derivatives markets."""

    open_interest_change: float | None = None
    funding_rate: float | None = None
    basis: float | None = None
    liquidation_long: float | None = None
    liquidation_short: float | None = None
    source: str = "unavailable"

    @property
    def available(self) -> bool:
        return any(
            value is not None
            for value in (
                self.open_interest_change,
                self.funding_rate,
                self.basis,
                self.liquidation_long,
                self.liquidation_short,
            )
        )

    @property
    def forced_flow_pressure(self) -> float:
        long_liq = max(0.0, self.liquidation_long or 0.0)
        short_liq = max(0.0, self.liquidation_short or 0.0)
        total = long_liq + short_liq
        if total <= 0:
            return 0.0
        return min(1.0, total / max(total + 1.0, 1.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_interest_change": self.open_interest_change,
            "funding_rate": self.funding_rate,
            "basis": self.basis,
            "liquidation_long": self.liquidation_long,
            "liquidation_short": self.liquidation_short,
            "forced_flow_pressure": round(self.forced_flow_pressure, 4),
            "source": self.source,
            "available": self.available,
        }


@dataclass(frozen=True)
class PriceLocationContext:
    vwap_distance: float = 0.0
    anchored_vwap_distances: Mapping[str, float] = field(default_factory=dict)
    poc_distance: float = 0.0
    value_area_location: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "vwap_distance": self.vwap_distance,
            "anchored_vwap_distances": dict(self.anchored_vwap_distances),
            "poc_distance": self.poc_distance,
            "value_area_location": self.value_area_location,
        }


@dataclass(frozen=True)
class ImbalanceZone:
    low: float
    high: float
    gap_size: float
    displacement_score: float
    fill_ratio: float = 0.0
    reaction_score: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "low": self.low,
            "high": self.high,
            "gap_size": self.gap_size,
            "displacement_score": self.displacement_score,
            "fill_ratio": self.fill_ratio,
            "reaction_score": self.reaction_score,
        }


@dataclass(frozen=True)
class OriginZone:
    low: float
    high: float
    displacement_score: float
    flow_confirmation: float
    liquidity_confirmation: float
    structure_confirmation: float
    quality: float

    def to_dict(self) -> dict[str, float]:
        return {
            "low": self.low,
            "high": self.high,
            "displacement_score": self.displacement_score,
            "flow_confirmation": self.flow_confirmation,
            "liquidity_confirmation": self.liquidity_confirmation,
            "structure_confirmation": self.structure_confirmation,
            "quality": self.quality,
        }


def positioning_context(
    *,
    open_interest_change: float | None = None,
    funding_rate: float | None = None,
    basis: float | None = None,
    liquidation_long: float | None = None,
    liquidation_short: float | None = None,
    source: str = "derivatives",
) -> PositioningContext:
    """Create a venue-neutral positioning context.

    Crypto feeds can populate all fields; equities/FX/futures adapters can
    populate whichever equivalent positioning observations they expose.
    """
    values = {
        "open_interest_change": open_interest_change,
        "funding_rate": funding_rate,
        "basis": basis,
        "liquidation_long": liquidation_long,
        "liquidation_short": liquidation_short,
    }
    for key, value in values.items():
        if value is not None and not isfinite(float(value)):
            values[key] = None
    return PositioningContext(source=source, **values)


def positioning_evidence(
    context: PositioningContext, direction: str
) -> dict[str, float]:
    """Map positioning observations to contextual 0..10 evidence scores."""
    sign = 1.0 if direction.lower() == "long" else -1.0
    scores: dict[str, float] = {}
    if context.open_interest_change is not None:
        oi = max(-1.0, min(1.0, context.open_interest_change * 10.0))
        scores["open_interest_positioning"] = 5.0 + 5.0 * oi * sign
    if context.funding_rate is not None:
        # Funding is treated as crowding context, not a direct directional rule.
        crowd = max(-1.0, min(1.0, context.funding_rate * 100.0))
        scores["funding_crowding"] = 5.0 - 2.5 * crowd * sign
    if context.basis is not None:
        basis = max(-1.0, min(1.0, context.basis * 20.0))
        scores["basis_context"] = 5.0 + 2.0 * basis * sign
    if context.available:
        scores["forced_flow"] = 5.0 + 5.0 * context.forced_flow_pressure
    return {k: round(max(0.0, min(10.0, v)), 4) for k, v in scores.items()}
