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
from .experiment_engine import (
    CostScenario,
    ExperimentEngine,
    ExperimentPlan,
    ExperimentReport,
    ExperimentVariant,
)
from .rollout import RolloutCoordinator

__all__ = [
    "ContinuousIntelligenceEngine",
    "ContinuousIntelligenceReport",
    "DriftDetector",
    "CostScenario",
    "ExperimentEngine",
    "ExperimentPlan",
    "ExperimentReport",
    "ExperimentVariant",
    "RolloutCoordinator",
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
