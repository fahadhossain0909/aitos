"""Adapter that feeds AITOS evidence into DecisionFusion during replay.

The adapter deliberately does not recreate live market intelligence. Callers
supply the same component scores produced by the live scanner/intelligence
pipeline for each replay timestamp, making differences in execution measurable
without silently using a second strategy implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aitos.kernel.decision_fusion import DecisionFusionEngine, EvidenceFusionResult


@dataclass(frozen=True)
class ReplayDecision:
    direction: str
    confidence: float
    authorized: bool
    fusion: EvidenceFusionResult


class AITOSReplayStrategy:
    def __init__(self, fusion: DecisionFusionEngine | None = None) -> None:
        self.fusion = fusion or DecisionFusionEngine()

    def decide(self, context: Mapping[str, Any]) -> ReplayDecision | None:
        result = self.fusion.fuse_context(context)
        if result is None:
            return None
        return ReplayDecision(
            direction=result.direction,
            confidence=result.confidence,
            authorized=result.direction in {"long", "short"},
            fusion=result,
        )
