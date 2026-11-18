"""Explainable address-level capital-flow candidate prioritization.

Scores are deterministic research heuristics, not probabilities, ownership claims,
investment advice, or trading signals. Aggregate exchange cohorts are excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, log1p
from statistics import mean
from typing import Iterable, Mapping

from .contracts import CapitalEvent


@dataclass(frozen=True, slots=True)
class CapitalCandidateRank:
    entity_id: str
    score: float
    gross_flow_usd: float
    net_flow_usd: float
    event_count: int
    unique_counterparties: int
    source_count: int
    direction_coverage: float
    market_impact_ratio: float | None
    evidence_confidence: float
    component_scores: Mapping[str, float]
    caveats: tuple[str, ...]


def rank_capital_candidates(
    events: Iterable[CapitalEvent],
    *,
    market_liquidity_usd: Mapping[str, float] | None = None,
    gross_flow_reference_usd: float = 1_000_000.0,
    minimum_events: int = 2,
) -> list[CapitalCandidateRank]:
    """Rank observed addresses for analyst review using explicit, inspectable factors."""
    if not isfinite(gross_flow_reference_usd) or gross_flow_reference_usd <= 0:
        raise ValueError("gross flow reference must be positive and finite")
    if minimum_events < 1:
        raise ValueError("minimum_events must be positive")

    grouped: dict[str, list[tuple[CapitalEvent, int, str | None]]] = {}
    null_evm_addresses = {
        "0x" + "0" * 40,
        "0x000000000000000000000000000000000000dead",
    }
    for event in events:
        identity_kind = event.metadata.get("actor_identity_kind")
        if (
            identity_kind not in {"evm_address", "solana_address"}
            or event.metadata.get("actor_is_pool_caller_not_verified_beneficial_owner")
            is True
            or event.metadata.get("aggregate_only") is True
            or event.source_kind == "exchange_aggregate"
            or event.usd_value is None
            or event.usd_value <= 0
        ):
            continue
        sign = event.metadata.get("flow_sign", 0)
        if sign not in (-1, 0, 1):
            raise ValueError("flow_sign must be -1, 0, or 1")

        def is_candidate_address(address: str | None) -> bool:
            if not address:
                return False
            return not (
                identity_kind == "evm_address" and address.lower() in null_evm_addresses
            )

        if is_candidate_address(event.actor_id):
            actor_id = f"{event.venue}:{event.actor_id}"
            actor_counterparty = (
                event.counterparty_id
                if is_candidate_address(event.counterparty_id)
                else None
            )
            grouped.setdefault(actor_id, []).append((event, sign, actor_counterparty))
        # A transfer has two address-level perspectives. The recipient's
        # direction is the inverse of the sender's; this is flow accounting,
        # not an inference about trade intent or beneficial ownership.
        if (
            is_candidate_address(event.counterparty_id)
            and event.counterparty_id != event.actor_id
        ):
            counterparty_id = f"{event.venue}:{event.counterparty_id}"
            recipient_counterparty = (
                event.actor_id if is_candidate_address(event.actor_id) else None
            )
            grouped.setdefault(counterparty_id, []).append(
                (event, -sign, recipient_counterparty)
            )

    ranked: list[CapitalCandidateRank] = []
    for entity_id, selected in grouped.items():
        if len(selected) < minimum_events:
            continue
        gross = sum(float(event.usd_value or 0) for event, _, _ in selected)
        net = 0.0
        directed = 0
        counterparties = set()
        sources = set()
        confidences = []
        for event, sign, counterparty_id in selected:
            if sign:
                directed += 1
                net += float(event.usd_value or 0) * sign
            if counterparty_id:
                counterparties.add(counterparty_id)
            sources.add(event.source_id)
            confidences.append(event.confidence)

        liquidity_values = [
            market_liquidity_usd[event.instrument]
            for event, _, _ in selected
            if market_liquidity_usd and event.instrument in market_liquidity_usd
        ]
        liquidity = min(liquidity_values) if liquidity_values else None
        if liquidity is not None and (
            not isfinite(float(liquidity)) or float(liquidity) <= 0
        ):
            raise ValueError("market liquidity values must be positive and finite")
        impact = gross / float(liquidity) if liquidity is not None else None
        direction_coverage = directed / len(selected)
        confidence = mean(confidences)

        components = {
            "gross_flow": min(log1p(gross) / log1p(gross_flow_reference_usd), 1.0),
            "recurrence": min(len(selected) / 10.0, 1.0),
            "counterparty_breadth": min(len(counterparties) / 5.0, 1.0),
            "source_diversity": min(len(sources) / 3.0, 1.0),
            "direction_coverage": direction_coverage,
            "liquidity_impact": (
                min(impact / 0.05, 1.0) if impact is not None else 0.0
            ),
            "evidence_confidence": confidence,
        }
        weights = {
            "gross_flow": 25.0,
            "recurrence": 15.0,
            "counterparty_breadth": 10.0,
            "source_diversity": 10.0,
            "direction_coverage": 15.0,
            "liquidity_impact": 15.0,
            "evidence_confidence": 10.0,
        }
        score = round(sum(components[key] * weights[key] for key in weights), 2)
        caveats = [
            "Heuristic analyst-prioritization score; not a probability or trading signal.",
            "Observed address activity does not establish beneficial ownership or intent.",
            "Source identifiers may not represent statistically independent evidence.",
        ]
        if liquidity is None:
            caveats.append(
                "Market liquidity was unavailable; impact component is zero."
            )
        if direction_coverage < 1:
            caveats.append(
                "Some events have unknown direction and are not assigned buy/sell intent."
            )
        if any(
            "USDT proxy" in str(event.metadata.get("usd_valuation_basis", ""))
            for event, _, _ in selected
        ):
            caveats.append("Some USD values use an explicitly enabled USDT proxy.")
        ranked.append(
            CapitalCandidateRank(
                entity_id,
                score,
                gross,
                net,
                len(selected),
                len(counterparties),
                len(sources),
                direction_coverage,
                impact,
                confidence,
                components,
                tuple(caveats),
            )
        )
    return sorted(ranked, key=lambda item: (-item.score, item.entity_id))
