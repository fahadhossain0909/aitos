"""Continual-learning primitives shared by backtest, paper, and live stages."""

from .evolution import EvolutionEngine, EvolutionProposal
from .experience import ExperienceRecord
from .model_registry import ModelArtifact, ModelRegistry

__all__ = [
    "ExperienceRecord",
    "ModelArtifact",
    "ModelRegistry",
    "EvolutionProposal",
    "EvolutionEngine",
]
