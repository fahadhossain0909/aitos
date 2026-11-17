"""Explainable CFEI entity and behavior intelligence primitives.

Scores are ranking heuristics, never calibrated probabilities or trading signals.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import mean
from typing import Iterable
from .contracts import CapitalEvent, EntityEvidence

@dataclass(frozen=True, slots=True)
class EntityLinkCandidate:
    left_id: str
    right_id: str
    relation: str
    confidence: float
    evidence_refs: tuple[str, ...]
    reasons: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class IntentHypothesis:
    label: str
    score: float
    evidence: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class BehavioralProfile:
    entity_id: str
    event_count: int
    net_flow_usd: float
    gross_flow_usd: float
    accumulation_score: float
    distribution_score: float
    flow_acceleration: float
    confidence: float
    hypotheses: tuple[IntentHypothesis, ...]

def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v)) if isfinite(v) else 0.0

def candidate_links_from_shared_funding(events: Iterable[CapitalEvent], *,
                                         minimum_repeated_transfers: int = 3) -> list[EntityLinkCandidate]:
    """Suggest links only; shared counterparties never prove common ownership."""
    if minimum_repeated_transfers < 2:
        raise ValueError("minimum_repeated_transfers must be >= 2")
    counts: dict[tuple[str, str], list[str]] = {}
    for event in events:
        if event.event_type not in {"token_transfer", "native_transfer"}:
            continue
        if not event.actor_id or not event.counterparty_id:
            continue
        pair = tuple(sorted((event.actor_id, event.counterparty_id)))
        counts.setdefault(pair, []).append(event.event_id)
    result = []
    for (left, right), refs in sorted(counts.items()):
        if len(refs) < minimum_repeated_transfers:
            continue
        confidence = _clamp(0.15 + 0.1 * min(len(refs), 5))
        result.append(EntityLinkCandidate(left, right, "repeated_transfer_counterparty",
                     confidence, tuple(sorted(set(refs))),
                     ("repeated direct transfers observed", "common ownership not established")))
    return result

def assess_entity_behavior(entity_id: str, events: Iterable[CapitalEvent], *,
                           market_liquidity_usd: float | None = None) -> BehavioralProfile:
    rows = sorted((e for e in events if e.actor_id == entity_id and e.usd_value is not None
                   and e.finality != "reverted"), key=lambda e: e.event_time)
    signed = []
    for event in rows:
        direction = event.metadata.get("flow_direction")
        if direction not in {"in", "out"}:
            direction = "out" if event.event_type in {"exchange_deposit", "distribution"} else "in"
        signed.append(float(event.usd_value) * (1 if direction == "in" else -1))
    net = sum(signed)
    gross = sum(abs(v) for v in signed)
    positive = sum(v for v in signed if v > 0)
    negative = -sum(v for v in signed if v < 0)
    denom = gross or 1.0
    accumulation = _clamp(positive / denom)
    distribution = _clamp(negative / denom)
    midpoint = max(1, len(signed)//2)
    first = sum(signed[:midpoint])
    second = sum(signed[midpoint:])
    acceleration = (second - first) / max(gross, 1.0)
    confidence_values = [e.confidence for e in rows]
    confidence = mean(confidence_values) if confidence_values else 0.0
    impact = gross / market_liquidity_usd if market_liquidity_usd and market_liquidity_usd > 0 else 0.0
    hypotheses = []
    if accumulation >= 0.65 and gross > 0:
        hypotheses.append(IntentHypothesis("accumulation_candidate", _clamp(accumulation*confidence), ("net positive observed flow",)))
    if distribution >= 0.65 and gross > 0:
        hypotheses.append(IntentHypothesis("distribution_candidate", _clamp(distribution*confidence), ("net negative observed flow",)))
    if impact >= 0.05:
        hypotheses.append(IntentHypothesis("market_impact_watch", _clamp(impact), ("gross flow is material versus supplied liquidity",)))
    if not hypotheses:
        hypotheses.append(IntentHypothesis("unknown", 0.0, ("insufficient directional evidence",)))
    return BehavioralProfile(entity_id, len(rows), net, gross, accumulation, distribution,
                             acceleration, confidence, tuple(hypotheses))

def evidence_from_candidate(candidate: EntityLinkCandidate, *, observed_at: datetime) -> EntityEvidence:
    """Persist as candidate only; this function cannot create verified ownership."""
    return EntityEvidence(candidate.left_id, candidate.right_id, candidate.relation,
                          "onchain_repeated_counterparty", ",".join(candidate.evidence_refs),
                          observed_at, candidate.confidence, "candidate")
