"""Strategy/model evolution proposals.

Evolution proposes changes; the canonical backtest/validation stack decides
whether a proposal is good enough to become a candidate model. No component in
this module can promote a model to production by itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class EvolutionProposal:
    model_name: str
    parent_version: str
    change_type: str
    change: dict[str, Any]
    rationale: str
    evidence_ids: tuple[str, ...] = ()
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EvolutionEngine:
    """Generate bounded, auditable candidate proposals from experience data."""

    def __init__(
        self,
        proposer: Callable[[list[dict[str, Any]]], EvolutionProposal] | None = None,
    ) -> None:
        self._proposer = proposer

    def propose(self, experiences: list[dict[str, Any]]) -> EvolutionProposal | None:
        if not experiences or self._proposer is None:
            return None
        proposal = self._proposer(experiences)
        if proposal.parent_version == proposal.change.get("target_version"):
            raise ValueError("proposal target must be a new version")
        return proposal

    @staticmethod
    def validate_proposal(proposal: EvolutionProposal) -> None:
        if proposal.change_type not in {"weight", "parameter", "rule", "architecture"}:
            raise ValueError("unsupported change_type")
        if not proposal.rationale.strip():
            raise ValueError("evolution proposal requires rationale")
        if not proposal.change:
            raise ValueError("evolution proposal cannot be empty")
