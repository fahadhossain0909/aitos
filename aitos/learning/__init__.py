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
from .experiment_engine import (
    CostScenario,
    ExperimentEngine,
    ExperimentPlan,
    ExperimentReport,
    ExperimentVariant,
)
from .governance import PromotionApproval, validation_evidence_digest
from .model_registry import ModelArtifact, ModelRegistry
from .rollout import RolloutCoordinator

__all__ = [
    "ContinuousIntelligenceEngine",
    "ContinuousIntelligenceReport",
    "CostScenario",
    "DriftDetector",
    "EvolutionEngine",
    "EvolutionProposal",
    "ExperienceRecord",
    "ExperimentEngine",
    "ExperimentPlan",
    "ExperimentReport",
    "ExperimentVariant",
    "ModelArtifact",
    "ModelRegistry",
    "PromotionApproval",
    "ResearchHypothesis",
    "RolloutCoordinator",
    "TradeObservation",
    "validation_evidence_digest",
]
