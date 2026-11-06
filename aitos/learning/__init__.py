"""Continual-learning primitives shared by backtest, paper, and live stages."""

from .continuous_intelligence import (
    ContinuousIntelligenceEngine,
    ContinuousIntelligenceReport,
    DriftDetector,
    ResearchHypothesis,
    TradeObservation,
)
from .evolution import EvolutionEngine, EvolutionProposal
from .experience import ExperienceRecord
from .governance import PromotionApproval, validation_evidence_digest
from .model_registry import ModelArtifact, ModelRegistry

__all__ = [
    "ContinuousIntelligenceEngine",
    "ContinuousIntelligenceReport",
    "DriftDetector",
    "EvolutionEngine",
    "EvolutionProposal",
    "ExperienceRecord",
    "ModelArtifact",
    "ModelRegistry",
    "PromotionApproval",
    "ResearchHypothesis",
    "TradeObservation",
    "validation_evidence_digest",
]
