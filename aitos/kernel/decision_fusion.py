"""Evidence-based decision fusion for the AITOS trading brain."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_EVIDENCE_WEIGHTS: dict[str, float] = {
    "trend_strength": 0.14,
    "liquidity_quality": 0.10,
    "order_flow_bias": 0.14,
    "auction_context": 0.10,
    "volatility": 0.05,
    "market_regime": 0.09,
    "lead_lag": 0.09,
    "funding_rate": 0.08,
    "open_interest_trend": 0.08,
    "rl_confidence": 0.04,
    "footprint_interaction": 0.09,
    "graph_historical_support": 0.05,
}

@dataclass(frozen=True)
class EvidenceContribution:
    source: str
    score: float
    weight: float
    weighted_score: float
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "score": self.score, "weight": self.weight, "weighted_score": self.weighted_score, "available": self.available}

@dataclass(frozen=True)
class EvidenceFusionResult:
    direction: str
    confidence: float
    contributions: tuple[EvidenceContribution, ...]
    missing_components: tuple[str, ...]

    @property
    def supported(self) -> bool:
        return self.confidence > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, "confidence": self.confidence, "contributions": [c.to_dict() for c in self.contributions], "missing_components": list(self.missing_components)}

class DecisionFusionEngine:
    """Fuse directional evidence; graph history is optional context evidence."""

    def __init__(self, weights: Mapping[str, float] | None = None, min_confidence: float = 0.60) -> None:
        selected = dict(weights or DEFAULT_EVIDENCE_WEIGHTS)
        if not selected or any(weight < 0 for weight in selected.values()):
            raise ValueError("Fusion weights must be non-empty and non-negative")
        if sum(selected.values()) <= 0:
            raise ValueError("Fusion weights must have a positive total")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self._weights = selected
        self._min_confidence = min_confidence

    @property
    def weights(self) -> dict[str, float]:
        return dict(self._weights)

    @property
    def min_confidence(self) -> float:
        return self._min_confidence

    @staticmethod
    def _normalize_direction(direction: str) -> str:
        if not isinstance(direction, str):
            raise ValueError(f"Unsupported direction: {direction}")
        normalized = direction.strip().lower()
        normalized = {"buy": "long", "sell": "short"}.get(normalized, normalized)
        if normalized not in {"long", "short", "neutral"}:
            raise ValueError(f"Unsupported direction: {direction}")
        return normalized

    def fuse(self, direction: str, component_scores: Mapping[str, Any], component_availability: Mapping[str, bool] | None = None) -> EvidenceFusionResult:
        direction = self._normalize_direction(direction)
        if direction == "neutral":
            return EvidenceFusionResult("neutral", 0.0, (), tuple(self._weights))
        availability = dict(component_availability or {})
        contributions: list[EvidenceContribution] = []
        missing: list[str] = []
        denominator = numerator = 0.0
        for name, weight in self._weights.items():
            raw = component_scores.get(name)
            is_available = availability.get(name, True)
            if raw is None or is_available is False or not isinstance(raw, (int, float)):
                missing.append(name)
                contributions.append(EvidenceContribution(name, 0.0, weight, 0.0, False))
                continue
            score = max(0.0, min(10.0, float(raw)))
            contributions.append(EvidenceContribution(name, round(score, 4), weight, round(score * weight, 4), True))
            numerator += score * weight
            denominator += weight
        confidence = (numerator / denominator) / 10.0 if denominator else 0.0
        fused_direction = direction if confidence >= self._min_confidence else "neutral"
        return EvidenceFusionResult(fused_direction, round(confidence, 4), tuple(contributions), tuple(missing))

    def fuse_context(self, context: Mapping[str, Any]) -> EvidenceFusionResult | None:
        direction = context.get("direction")
        component_scores = context.get("component_scores")
        if not isinstance(direction, str) or not isinstance(component_scores, Mapping):
            return None
        availability = context.get("component_availability")
        if availability is not None and not isinstance(availability, Mapping):
            availability = None
        return self.fuse(direction, component_scores, availability)
