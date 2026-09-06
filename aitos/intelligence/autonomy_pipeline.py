"""Governed autonomy primitives for AITOS.

This module imports the architectural discipline of AITROS without coupling the
trading core to an agent framework.  It makes the decision lifecycle explicit:
Evidence -> Knowledge -> Decision -> Policy -> Execution Intent -> Outcome.

The pipeline is deterministic, fail-closed, serialisable, and replay-friendly.
Execution is represented by an intent only; venue adapters remain responsible
for placing orders.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal

Action = Literal["long", "short", "no_trade"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class EvidenceItem:
    source: str
    value: float
    reliability: float = 1.0
    freshness_seconds: float = 0.0
    available: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return (
            self.available and self.reliability > 0.0 and self.freshness_seconds >= 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgeSnapshot:
    market_state: str
    regime: str
    evidence: tuple[EvidenceItem, ...]
    features: Mapping[str, float] = field(default_factory=dict)
    prior_context: Mapping[str, Any] = field(default_factory=dict)

    @property
    def evidence_hash(self) -> str:
        return _stable_hash(
            {
                "state": self.market_state,
                "evidence": [e.to_dict() for e in self.evidence],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_state": self.market_state,
            "regime": self.regime,
            "evidence": [e.to_dict() for e in self.evidence],
            "features": dict(self.features),
            "prior_context": dict(self.prior_context),
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class DecisionRecord:
    action: Action
    confidence: float
    rationale: tuple[str, ...]
    knowledge_hash: str
    policy_version: str
    decision_id: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionIntent:
    action: Action
    instrument: str
    quantity: float
    confidence: float
    decision_id: str
    policy_version: str
    risk_approved: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyResult:
    allowed: bool
    reasons: tuple[str, ...] = ()
    policy_version: str = "baseline"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LearningOutcome:
    decision_id: str
    action: Action
    pnl: float
    success: bool
    regime: str
    lessons: tuple[str, ...] = ()
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AutonomyRecord:
    knowledge: KnowledgeSnapshot
    decision: DecisionRecord
    policy: PolicyResult
    intent: ExecutionIntent

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge": self.knowledge.to_dict(),
            "decision": self.decision.to_dict(),
            "policy": self.policy.to_dict(),
            "intent": self.intent.to_dict(),
        }


class FailClosedPolicy:
    """Safety policy that rejects unsafe or incomplete decisions by default."""

    def __init__(
        self,
        *,
        max_staleness_seconds: float = 30.0,
        min_confidence: float = 0.60,
        require_evidence: bool = True,
    ) -> None:
        if max_staleness_seconds < 0:
            raise ValueError("max_staleness_seconds must be non-negative")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self.max_staleness_seconds = max_staleness_seconds
        self.min_confidence = min_confidence
        self.require_evidence = require_evidence

    def evaluate(
        self,
        knowledge: KnowledgeSnapshot,
        action: Action,
        confidence: float,
        *,
        policy_version: str = "baseline",
        risk_approved: bool = True,
    ) -> PolicyResult:
        reasons: list[str] = []
        usable = [e for e in knowledge.evidence if e.usable]
        if self.require_evidence and not usable:
            reasons.append("no usable evidence")
        if action not in {"long", "short", "no_trade"}:
            reasons.append("invalid action")
        if action != "no_trade" and confidence < self.min_confidence:
            reasons.append("confidence below policy floor")
        if any(e.freshness_seconds > self.max_staleness_seconds for e in usable):
            reasons.append("stale evidence")
        if not risk_approved and action != "no_trade":
            reasons.append("risk gate rejected")
        return PolicyResult(not reasons, tuple(reasons), policy_version)


class AutonomyPipeline:
    """Build auditable decision records and execution intents.

    ``decision_fn`` receives a KnowledgeSnapshot and returns ``(action,
    confidence, rationale)``.  A risk callback may veto an otherwise valid
    action.  No function in this class talks to an exchange or broker.
    """

    def __init__(
        self,
        *,
        policy: FailClosedPolicy | None = None,
        decision_fn: (
            Callable[[KnowledgeSnapshot], tuple[Action, float, Sequence[str]]] | None
        ) = None,
        policy_version: str = "baseline",
    ) -> None:
        self.policy = policy or FailClosedPolicy()
        self.decision_fn = decision_fn or self._default_decision
        self.policy_version = policy_version
        self.history: list[AutonomyRecord] = []
        self.outcomes: list[LearningOutcome] = []

    @staticmethod
    def _default_decision(
        knowledge: KnowledgeSnapshot,
    ) -> tuple[Action, float, Sequence[str]]:
        usable = [e for e in knowledge.evidence if e.usable]
        if not usable:
            return "no_trade", 0.0, ("no usable evidence",)
        weighted = sum(e.value * e.reliability for e in usable)
        weight = sum(e.reliability for e in usable) or 1.0
        score = weighted / weight
        confidence = min(1.0, abs(score))
        if confidence < 0.5:
            return "no_trade", confidence, ("evidence is not directionally decisive",)
        action: Action = "long" if score > 0 else "short"
        return (
            action,
            confidence,
            (f"weighted_evidence={score:.4f}", f"regime={knowledge.regime}"),
        )

    def decide(
        self,
        knowledge: KnowledgeSnapshot,
        *,
        instrument: str,
        quantity: float = 0.0,
        risk_approved: bool = True,
    ) -> AutonomyRecord:
        action, confidence, rationale = self.decision_fn(knowledge)
        confidence = max(0.0, min(1.0, float(confidence)))
        decision_id = _stable_hash(
            {
                "knowledge": knowledge.evidence_hash,
                "action": action,
                "confidence": round(confidence, 8),
                "policy": self.policy_version,
            }
        )
        decision = DecisionRecord(
            action=action,
            confidence=confidence,
            rationale=tuple(str(x) for x in rationale),
            knowledge_hash=knowledge.evidence_hash,
            policy_version=self.policy_version,
            decision_id=decision_id,
            created_at=_utc_now(),
        )
        policy_result = self.policy.evaluate(
            knowledge,
            action,
            confidence,
            policy_version=self.policy_version,
            risk_approved=risk_approved,
        )
        intent_action: Action = action if policy_result.allowed else "no_trade"
        intent = ExecutionIntent(
            action=intent_action,
            instrument=str(instrument),
            quantity=float(quantity) if intent_action != "no_trade" else 0.0,
            confidence=confidence if intent_action != "no_trade" else 0.0,
            decision_id=decision_id,
            policy_version=self.policy_version,
            risk_approved=policy_result.allowed,
            reason=(
                "approved"
                if policy_result.allowed
                else "; ".join(policy_result.reasons)
            ),
        )
        record = AutonomyRecord(knowledge, decision, policy_result, intent)
        self.history.append(record)
        return record

    def learn(self, outcome: LearningOutcome) -> None:
        """Store an outcome; learning remains explicit and cannot mutate policy."""
        if not any(r.decision.decision_id == outcome.decision_id for r in self.history):
            raise ValueError("outcome references an unknown decision")
        self.outcomes.append(outcome)

    def replay(self, record: AutonomyRecord) -> dict[str, Any]:
        """Return a deterministic replay payload for audit/regression tests."""
        return record.to_dict()

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "decisions": len(self.history),
            "outcomes": len(self.outcomes),
            "last_decision": (
                self.history[-1].decision.to_dict() if self.history else None
            ),
        }
