"""Governed promotion and rollback for learned policy candidates.

Promotion is explicit and guarded by a shadow-evaluation result. This module
never mutates the live fusion engine implicitly; callers must explicitly
approve a promotion and persist the resulting active policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from .evidence_shadow import ShadowWeightResult


@dataclass(frozen=True)
class PolicyVersion:
    version: str
    weights: Dict[str, float]
    created_at: str
    source: str = "baseline"


@dataclass
class PolicyGovernance:
    active: PolicyVersion
    history: list[PolicyVersion] = field(default_factory=list)

    def propose_promotion(
        self,
        candidate_version: str,
        candidate_weights: Mapping[str, float],
        shadow: ShadowWeightResult,
        *,
        approved: bool = False,
    ) -> PolicyVersion:
        if not shadow.eligible:
            raise ValueError(f"candidate is not eligible: {shadow.reason}")
        if not approved:
            raise PermissionError("explicit approval is required for promotion")
        weights = {str(k): float(v) for k, v in candidate_weights.items()}
        if not weights or any(v < 0 for v in weights.values()):
            raise ValueError("candidate policy contains invalid weights")
        if abs(sum(weights.values()) - 1.0) > 1e-6:
            raise ValueError("candidate weights must sum to 1")
        promoted = PolicyVersion(
            version=str(candidate_version),
            weights=weights,
            created_at=datetime.now(timezone.utc).isoformat(),
            source="shadow-approved",
        )
        self.history.append(self.active)
        self.active = promoted
        return promoted

    def rollback(self) -> PolicyVersion:
        if not self.history:
            raise RuntimeError("no previous policy available for rollback")
        previous = self.history.pop()
        self.active = previous
        return previous

    def snapshot(self) -> Dict[str, Any]:
        return {
            "active": {
                "version": self.active.version,
                "weights": dict(self.active.weights),
                "created_at": self.active.created_at,
                "source": self.active.source,
            },
            "history_depth": len(self.history),
        }
