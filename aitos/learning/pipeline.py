"""Shared learning lifecycle: experience -> proposal -> backtest -> validation.

Execution stages remain isolated, while their knowledge flows through this
pipeline. A candidate is never promoted automatically by this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from aitos.backtest.engine import BacktestResult

from .evolution import EvolutionEngine, EvolutionProposal
from .model_registry import ModelArtifact, ModelRegistry
from .validation import CandidateValidator, ValidationResult


@dataclass(frozen=True)
class EvolutionEvaluation:
    proposal: EvolutionProposal
    validation: ValidationResult
    artifact: ModelArtifact | None


class ContinualLearningPipeline:
    """Coordinate candidate evolution against the canonical backtest engine."""

    def __init__(
        self,
        registry: ModelRegistry,
        evolution: EvolutionEngine,
        validator: CandidateValidator,
    ) -> None:
        self.registry = registry
        self.evolution = evolution
        self.validator = validator

    def evaluate_proposal(
        self,
        proposal: EvolutionProposal,
        events: Iterable[Any],
        strategy: Callable,
        mark_price: Callable,
        champion_result: BacktestResult | None,
        initial_cash: float = 10_000.0,
        fee_rate: float = 0.0004,
        slippage_bps: float = 0.0,
        candidate_version: str | None = None,
    ) -> EvolutionEvaluation:
        self.evolution.validate_proposal(proposal)
        validation = self.validator.evaluate(
            events,
            strategy,
            mark_price,
            champion_result,
            initial_cash,
            fee_rate,
            slippage_bps,
        )
        version = candidate_version or proposal.change.get("target_version")
        artifact = None
        if validation.passed and version:
            metrics = validation.candidate.metrics
            artifact = self.registry.register(
                ModelArtifact(
                    name=proposal.model_name,
                    version=str(version),
                    kind=proposal.change_type,
                    status="candidate",
                    parent_version=proposal.parent_version,
                    training_data_id=proposal.proposal_id,
                    metrics={
                        "total_return": metrics.total_return,
                        "max_drawdown": metrics.max_drawdown,
                        "sharpe": metrics.sharpe,
                        "profit_factor": metrics.profit_factor,
                    },
                    metadata={
                        "proposal_id": proposal.proposal_id,
                        "rationale": proposal.rationale,
                    },
                )
            )
        return EvolutionEvaluation(proposal, validation, artifact)
