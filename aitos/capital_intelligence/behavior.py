"""Descriptive capital-flow features; never a standalone trading signal."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean
from typing import Iterable

from .contracts import CapitalEvent


@dataclass(frozen=True, slots=True)
class BehaviorAssessment:
    entity_id: str
    net_flow_usd: float
    gross_flow_usd: float
    event_count: int
    accumulation_score: float
    distribution_score: float
    market_impact_ratio: float | None
    evidence_confidence: float
    classification: str
    caveats: tuple[str, ...]


def assess_capital_behavior(
    entity_id: str,
    events: Iterable[CapitalEvent],
    *,
    market_liquidity_usd: float | None = None,
) -> BehaviorAssessment:
    if not entity_id.strip():
        raise ValueError("entity_id is required")
    if ":" in entity_id:
        venue, address = entity_id.split(":", 1)

        def matches_entity(event):
            return event.venue == venue and event.actor_id == address

    else:
        # Backward-compatible raw-address lookup; new callers should use
        # chain-scoped IDs returned by the entity explorer/ranking API.
        def matches_entity(event):
            return event.actor_id == entity_id
    selected = [
        event
        for event in events
        if matches_entity(event) and event.usd_value is not None
    ]
    if not selected:
        raise ValueError("valued events for entity are required")

    signed = []
    gross = []
    confidence = []
    for event in selected:
        value = float(event.usd_value or 0)
        sign = event.metadata.get("flow_sign", 0)
        if sign not in (-1, 0, 1):
            raise ValueError("flow_sign must be -1, 0, or 1")
        gross.append(value)
        signed.append(value * sign)
        confidence.append(event.confidence)

    total = sum(gross)
    net = sum(signed)
    accumulation = max(0.0, net) / total if total else 0.0
    distribution = max(0.0, -net) / total if total else 0.0
    impact = None
    if market_liquidity_usd is not None:
        liquidity = float(market_liquidity_usd)
        if not isfinite(liquidity) or liquidity <= 0:
            raise ValueError("market liquidity must be positive")
        impact = total / liquidity

    label = (
        "accumulation_candidate"
        if accumulation >= 0.6
        else (
            "distribution_candidate" if distribution >= 0.6 else "mixed_or_unclassified"
        )
    )
    return BehaviorAssessment(
        entity_id,
        net,
        total,
        len(selected),
        accumulation,
        distribution,
        impact,
        mean(confidence),
        label,
        (
            "Transfer direction is not equivalent to buy/sell intent.",
            "CEX internal positions and unobserved wallets are not represented.",
            "Descriptive evidence only; not a calibrated forecast or order instruction.",
        ),
    )
