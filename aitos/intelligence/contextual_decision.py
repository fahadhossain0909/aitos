"""AI-based contextual decision support for AITOS.

The engine is deterministic and model-agnostic by design.  It prepares a
structured context that an LLM, ML model, RL policy or the existing decision
fusion layer can consume.  It never turns an uncertain market observation
into a forced BUY/SELL decision and treats NO_TRADE as a first-class outcome.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aitos.intelligence.advanced_context import AdvancedMarketContext


@dataclass(frozen=True)
class Scenario:
    name: str
    probability: float
    direction: str
    target_score: float
    invalidation_score: float
    required_confirmation: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "probability": round(self.probability, 4),
            "direction": self.direction,
            "target_score": round(self.target_score, 4),
            "invalidation_score": round(self.invalidation_score, 4),
            "required_confirmation": list(self.required_confirmation),
        }


@dataclass(frozen=True)
class EvidenceAssessment:
    source: str
    score: float
    reliability: float
    relevance: float
    signed_support: float

    @property
    def effective_support(self) -> float:
        return self.signed_support * self.reliability * self.relevance

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "score": round(self.score, 4),
            "reliability": round(self.reliability, 4),
            "relevance": round(self.relevance, 4),
            "effective_support": round(self.effective_support, 4),
        }


@dataclass(frozen=True)
class ContextualDecision:
    action: str
    confidence: float
    market_state: str
    scenarios: tuple[Scenario, ...]
    evidence: tuple[EvidenceAssessment, ...]
    contradictions: tuple[str, ...]
    invalidations: tuple[str, ...]
    target_zones: tuple[float, ...]
    rationale: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "confidence": round(self.confidence, 4),
            "market_state": self.market_state,
            "scenarios": [x.to_dict() for x in self.scenarios],
            "evidence": [x.to_dict() for x in self.evidence],
            "contradictions": list(self.contradictions),
            "invalidations": list(self.invalidations),
            "target_zones": list(self.target_zones),
            "rationale": list(self.rationale),
        }


class ContextualDecisionEngine:
    """Turn heterogeneous evidence into a disciplined decision context.

    The engine intentionally does not call an external LLM.  This makes the
    layer usable in paper/live infrastructure without API-cost or latency
    coupling.  An LLM/ML agent can consume ``ContextualDecision.to_dict()``
    later as a reasoning payload.
    """

    DEFAULT_RELIABILITY: Mapping[str, float] = {
        "trend_strength": 0.90,
        "liquidity_quality": 0.88,
        "order_flow_bias": 0.92,
        "auction_context": 0.90,
        "volatility": 0.86,
        "market_regime": 0.88,
        "lead_lag": 0.80,
        "funding_rate": 0.70,
        "open_interest_trend": 0.78,
        "rl_confidence": 0.75,
        "footprint_interaction": 0.93,
        "volume_profile": 0.84,
        "price_imbalance": 0.62,
        "structural_symmetry": 0.55,
        "forced_flow": 0.68,
    }

    def __init__(
        self, min_confidence: float = 0.60, no_trade_margin: float = 0.08
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if no_trade_margin < 0:
            raise ValueError("no_trade_margin must be non-negative")
        self.min_confidence = min_confidence
        self.no_trade_margin = no_trade_margin

    @staticmethod
    def _direction_score(value: float) -> float:
        """Map a 0..10 component to signed support in [-1, 1]."""
        return max(-1.0, min(1.0, (float(value) - 5.0) / 5.0))

    def assess_evidence(
        self,
        component_scores: Mapping[str, Any],
        direction: str,
        *,
        relevance: Mapping[str, float] | None = None,
        reliability: Mapping[str, float] | None = None,
    ) -> tuple[EvidenceAssessment, ...]:
        direction = direction.lower()
        sign = 1.0 if direction == "long" else -1.0
        relevance = relevance or {}
        reliability = reliability or self.DEFAULT_RELIABILITY
        result: list[EvidenceAssessment] = []
        for source, raw in component_scores.items():
            if not isinstance(raw, (int, float)):
                continue
            score = max(0.0, min(10.0, float(raw)))
            signed = self._direction_score(score) * sign
            result.append(
                EvidenceAssessment(
                    source=source,
                    score=score,
                    reliability=max(0.0, min(1.0, reliability.get(source, 0.70))),
                    relevance=max(0.0, min(1.0, relevance.get(source, 1.0))),
                    signed_support=signed,
                )
            )
        return tuple(result)

    def _market_state(self, context: Mapping[str, Any]) -> str:
        regime = str(context.get("regime", "unknown"))
        vol = context.get("volatility_regime") or context.get("volatility_state")
        if vol in {"compression", "expansion", "extreme"}:
            return f"{regime}:{vol}"
        return regime

    def build(
        self,
        *,
        direction: str,
        component_scores: Mapping[str, Any],
        component_availability: Mapping[str, bool] | None = None,
        context: Mapping[str, Any] | None = None,
        advanced: AdvancedMarketContext | None = None,
    ) -> ContextualDecision:
        direction = direction.lower()
        if direction not in {"long", "short"}:
            return ContextualDecision(
                "no_trade",
                0.0,
                "unknown",
                (),
                (),
                (),
                ("no directional thesis",),
                (),
                ("No directional thesis was supplied.",),
            )
        context = context or {}
        availability = component_availability or {}
        usable = {
            k: v
            for k, v in component_scores.items()
            if availability.get(k, True) and isinstance(v, (int, float))
        }
        evidence = self.assess_evidence(usable, direction)
        support = sum(x.effective_support for x in evidence)
        denom = sum(x.reliability * x.relevance for x in evidence) or 1.0
        directional_confidence = 0.5 + 0.5 * support / denom
        directional_confidence = max(0.0, min(1.0, directional_confidence))

        contradictions: list[str] = []
        positives = [x for x in evidence if x.effective_support > 0.15]
        negatives = [x for x in evidence if x.effective_support < -0.15]
        if positives and negatives:
            contradictions.append(
                "supporting and opposing evidence are materially mixed"
            )
        if advanced is not None:
            if advanced.symmetry and advanced.symmetry.similarity >= 0.65:
                if advanced.symmetry.failure_distance >= 1.5:
                    contradictions.append("historical symmetry is currently failing")
            if advanced.volatility.regime == "extreme":
                contradictions.append(
                    "extreme volatility reduces directional reliability"
                )
            if (
                advanced.volume_profile
                and advanced.volume_profile.acceptance_score < 0.35
            ):
                contradictions.append(
                    "price is not strongly accepted around the profile"
                )

        scenarios = self._scenarios(
            direction, directional_confidence, advanced, contradictions
        )
        best = scenarios[0] if scenarios else None
        action = (
            direction
            if best
            and best.probability >= self.min_confidence
            and not (
                len(contradictions) >= 2
                and best.probability < self.min_confidence + self.no_trade_margin
            )
            else "no_trade"
        )
        if action == "no_trade":
            confidence = min(directional_confidence, 0.59)
        else:
            confidence = best.probability

        targets: list[float] = []
        if advanced and advanced.symmetry:
            targets.extend(advanced.symmetry.projected_levels)
        if advanced and advanced.volume_profile:
            targets.extend(
                [
                    advanced.volume_profile.poc,
                    advanced.volume_profile.vah,
                    advanced.volume_profile.val,
                ]
            )
        targets = list(
            dict.fromkeys(round(x, 8) for x in targets if isinstance(x, (int, float)))
        )
        invalidations = [
            "opposing market-structure break",
            "confirmed order-flow reversal",
            "loss of liquidity/auction confirmation",
        ]
        if advanced and advanced.symmetry:
            invalidations.append("structural-symmetry failure beyond tolerance")

        rationale = [
            f"state={self._market_state(context)}",
            f"directional_confidence={directional_confidence:.3f}",
            f"evidence_sources={len(evidence)}",
            f"contradictions={len(contradictions)}",
            f"action={action}",
        ]
        return ContextualDecision(
            action=action,
            confidence=round(confidence, 4),
            market_state=self._market_state(context),
            scenarios=tuple(scenarios),
            evidence=evidence,
            contradictions=tuple(contradictions),
            invalidations=tuple(invalidations),
            target_zones=tuple(targets),
            rationale=tuple(rationale),
        )

    def _scenarios(
        self,
        direction: str,
        confidence: float,
        advanced: AdvancedMarketContext | None,
        contradictions: Sequence[str],
    ) -> list[Scenario]:
        opposite = "short" if direction == "long" else "long"
        symmetry_bonus = (
            advanced.symmetry.similarity * 0.08
            if advanced and advanced.symmetry
            else 0.0
        )
        continuation = max(
            0.0, min(1.0, confidence + symmetry_bonus - 0.04 * len(contradictions))
        )
        reversal = max(
            0.0, min(1.0, 1.0 - confidence + (0.08 if contradictions else 0.0))
        )
        range_prob = max(0.0, 1.0 - max(continuation, reversal))
        total = continuation + reversal + range_prob or 1.0
        scenarios = [
            Scenario(
                "continuation",
                continuation / total,
                direction,
                confidence,
                1.0 - confidence,
                ("flow confirmation", "structure remains intact"),
            ),
            Scenario(
                "reversal",
                reversal / total,
                opposite,
                1.0 - confidence,
                confidence,
                ("structural break", "opposing flow confirmation"),
            ),
            Scenario(
                "range",
                range_prob / total,
                "neutral",
                0.5,
                0.5,
                ("failed displacement", "balanced auction"),
            ),
        ]
        return sorted(scenarios, key=lambda x: x.probability, reverse=True)
