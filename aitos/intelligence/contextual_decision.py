"""Contextual Market Intelligence for AITOS.

The engine is deterministic and model-agnostic.  Quantitative features,
historical analogues, state transitions and competing scenarios are prepared
for an LLM/ML/RL policy; no external model is required and NO_TRADE is a
first-class outcome.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aitos.intelligence.advanced_context import AdvancedMarketContext
from aitos.intelligence.historical_analogue import (
    HistoricalAnalogue,
    infer_state_transition,
    search_historical_analogues,
)
from aitos.models.market import Kline


@dataclass(frozen=True)
class Scenario:
    name: str
    probability: float
    direction: str
    target_score: float
    invalidation_score: float
    required_confirmation: tuple[str, ...]
    expected_path: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "probability": round(self.probability, 4),
            "direction": self.direction,
            "target_score": round(self.target_score, 4),
            "invalidation_score": round(self.invalidation_score, 4),
            "required_confirmation": list(self.required_confirmation),
            "expected_path": list(self.expected_path),
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
    historical_analogues: tuple[HistoricalAnalogue, ...] = ()
    state_transition: dict[str, Any] | None = None

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
            "historical_analogues": [x.to_dict() for x in self.historical_analogues],
            "state_transition": self.state_transition,
        }


class ContextualDecisionEngine:
    """Fuse market evidence into a structured, auditable decision context."""

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
        "historical_analogue": 0.72,
        "state_transition": 0.78,
    }

    def __init__(self, min_confidence: float = 0.60, no_trade_margin: float = 0.08) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if no_trade_margin < 0:
            raise ValueError("no_trade_margin must be non-negative")
        self.min_confidence = min_confidence
        self.no_trade_margin = no_trade_margin

    @staticmethod
    def _direction_score(value: float) -> float:
        return max(-1.0, min(1.0, (float(value) - 5.0) / 5.0))

    @staticmethod
    def _parse_klines(raw: Any) -> tuple[Kline, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        result: list[Kline] = []
        for item in raw:
            if isinstance(item, Kline):
                result.append(item)
            elif isinstance(item, Mapping):
                try:
                    result.append(Kline.from_dict(dict(item)))
                except (KeyError, TypeError, ValueError):
                    continue
        return tuple(result)

    def assess_evidence(
        self,
        component_scores: Mapping[str, Any],
        direction: str,
        *,
        relevance: Mapping[str, float] | None = None,
        reliability: Mapping[str, float] | None = None,
    ) -> tuple[EvidenceAssessment, ...]:
        sign = 1.0 if direction.lower() == "long" else -1.0
        relevance = relevance or {}
        reliability = reliability or self.DEFAULT_RELIABILITY
        result: list[EvidenceAssessment] = []
        for source, raw in component_scores.items():
            if not isinstance(raw, (int, float)):
                continue
            score = max(0.0, min(10.0, float(raw)))
            result.append(
                EvidenceAssessment(
                    source=source,
                    score=score,
                    reliability=max(0.0, min(1.0, reliability.get(source, 0.70))),
                    relevance=max(0.0, min(1.0, relevance.get(source, 1.0))),
                    signed_support=self._direction_score(score) * sign,
                )
            )
        return tuple(result)

    def _market_state(self, context: Mapping[str, Any]) -> str:
        regime = str(context.get("regime", "unknown"))
        vol = context.get("volatility_regime") or context.get("volatility_state")
        return f"{regime}:{vol}" if vol in {"compression", "expansion", "extreme"} else regime

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
                "no_trade", 0.0, "unknown", (), (), (),
                ("no directional thesis",), (),
                ("No directional thesis was supplied.",),
            )
        context = context or {}
        availability = component_availability or {}
        usable = {
            k: v for k, v in component_scores.items()
            if availability.get(k, True) and isinstance(v, (int, float))
        }
        evidence = list(self.assess_evidence(usable, direction))
        contradictions: list[str] = []

        # Optional advanced quantitative context is promoted to evidence only
        # when the caller supplied it; no synthetic values are invented.
        if advanced is not None:
            derived = {
                "volume_profile": advanced.volume_profile.price_location * 10.0 if advanced.volume_profile else 5.0,
                "price_imbalance": 5.0 + advanced.imbalance.displacement_score * 5.0,
                "structural_symmetry": 5.0 + (advanced.symmetry.similarity * 5.0 if advanced.symmetry else 0.0),
                "forced_flow": advanced.forced_flow_score,
            }
            evidence.extend(self.assess_evidence(derived, direction))
            if advanced.volatility.regime == "extreme":
                contradictions.append("extreme volatility reduces directional reliability")
            if advanced.symmetry and advanced.symmetry.similarity >= 0.65 and advanced.symmetry.failure_distance >= 1.5:
                contradictions.append("historical symmetry is currently failing")
            if advanced.volume_profile and advanced.volume_profile.acceptance_score < 0.35:
                contradictions.append("price is not strongly accepted around the profile")

        klines = self._parse_klines(context.get("klines"))
        analogues = search_historical_analogues(
            klines,
            window=int(context.get("analogue_window", 20)),
            search_back=int(context.get("analogue_search_back", 500)),
            top_k=int(context.get("analogue_top_k", 20)),
            forward_horizon=int(context.get("analogue_horizon", 12)),
        ) if klines else ()
        if analogues:
            best_outcome = analogues[0].outcome
            if best_outcome:
                directional = best_outcome.up_probability if direction == "long" else best_outcome.down_probability
                evidence.append(EvidenceAssessment(
                    "historical_analogue", directional * 10.0, 0.72,
                    max(0.25, analogues[0].similarity), (directional - 0.5) * 2.0,
                ))

        previous_state = str(context.get("previous_market_state", context.get("previous_regime", "unknown")))
        transition = infer_state_transition(previous_state, self._market_state(context), float(context.get("state_persistence", 0.5)))
        transition_dict = transition.to_dict()
        if transition.transition_score and transition.reversal_pressure:
            contradictions.append("market-state transition carries reversal pressure")

        positives = [x for x in evidence if x.effective_support > 0.15]
        negatives = [x for x in evidence if x.effective_support < -0.15]
        if positives and negatives:
            contradictions.append("supporting and opposing evidence are materially mixed")
        support = sum(x.effective_support for x in evidence)
        denom = sum(x.reliability * x.relevance for x in evidence) or 1.0
        confidence = max(0.0, min(1.0, 0.5 + 0.5 * support / denom))
        scenarios = self._scenarios(direction, confidence, advanced, analogues, contradictions)
        best = scenarios[0]
        action = direction if best.probability >= self.min_confidence and not (
            len(contradictions) >= 2 and best.probability < self.min_confidence + self.no_trade_margin
        ) else "no_trade"
        final_confidence = best.probability if action != "no_trade" else min(confidence, 0.59)

        targets: list[float] = []
        if advanced and advanced.symmetry:
            targets.extend(advanced.symmetry.projected_levels)
        if advanced and advanced.volume_profile:
            targets.extend([advanced.volume_profile.poc, advanced.volume_profile.vah, advanced.volume_profile.val])
        for match in analogues[:3]:
            if match.outcome and klines:
                base = klines[-1].close
                targets.append(base * (1.0 + match.outcome.median_return))
        targets = list(dict.fromkeys(round(x, 8) for x in targets if isinstance(x, (int, float))))

        invalidations = [
            "opposing market-structure break",
            "confirmed order-flow reversal",
            "loss of liquidity/auction confirmation",
        ]
        if advanced and advanced.symmetry:
            invalidations.append("structural-symmetry failure beyond tolerance")
        rationale = (
            f"state={self._market_state(context)}",
            f"directional_confidence={confidence:.3f}",
            f"evidence_sources={len(evidence)}",
            f"historical_matches={len(analogues)}",
            f"state_transition={transition.transition_score:.1f}",
            f"contradictions={len(contradictions)}",
            f"action={action}",
        )
        return ContextualDecision(
            action, round(final_confidence, 4), self._market_state(context),
            tuple(scenarios), tuple(evidence), tuple(contradictions),
            tuple(invalidations), tuple(targets), rationale,
            tuple(analogues), transition_dict,
        )

    def _scenarios(
        self,
        direction: str,
        confidence: float,
        advanced: AdvancedMarketContext | None,
        analogues: Sequence[HistoricalAnalogue],
        contradictions: Sequence[str],
    ) -> list[Scenario]:
        opposite = "short" if direction == "long" else "long"
        analogue_bias = 0.0
        if analogues and analogues[0].outcome:
            o = analogues[0].outcome
            analogue_bias = (o.up_probability - o.down_probability) * 0.10
            if direction == "short":
                analogue_bias = -analogue_bias
        symmetry_bonus = advanced.symmetry.similarity * 0.08 if advanced and advanced.symmetry else 0.0
        continuation = max(0.0, min(1.0, confidence + analogue_bias + symmetry_bonus - 0.04 * len(contradictions)))
        reversal = max(0.0, min(1.0, 1.0 - confidence + 0.08 * bool(contradictions)))
        range_prob = max(0.0, 1.0 - max(continuation, reversal))
        total = continuation + reversal + range_prob or 1.0
        scenarios = [
            Scenario("continuation", continuation / total, direction, confidence, 1.0 - confidence,
                     ("flow confirmation", "structure remains intact"),
                     ("retest", "liquidity interaction", "continuation")),
            Scenario("reversal", reversal / total, opposite, 1.0 - confidence, confidence,
                     ("structural break", "opposing flow confirmation"),
                     ("failure", "reclaim/rejection", "reversal")),
            Scenario("range", range_prob / total, "neutral", 0.5, 0.5,
                     ("failed displacement", "balanced auction"),
                     ("mean reversion", "range continuation")),
        ]
        return sorted(scenarios, key=lambda x: x.probability, reverse=True)
