"""Governance-controlled rollback orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from .policy_governance import PolicyGovernance, PolicyVersion


@dataclass(frozen=True)
class RollbackRequest:
    request_id: str
    policy_version: str
    reason: str
    created_at: str
    approved: bool = False
    executed: bool = False


class PolicyRollbackService:
    def __init__(
        self,
        governance: PolicyGovernance,
        registry: Any,
        kernel: Any,
        event_bus: Any = None,
    ):
        self.governance = governance
        self.registry = registry
        self.kernel = kernel
        self.event_bus = event_bus
        self.requests: Dict[str, RollbackRequest] = {}

    def request(
        self, request_id: str, policy_version: str, reason: str
    ) -> RollbackRequest:
        req = RollbackRequest(
            request_id, policy_version, reason, datetime.now(timezone.utc).isoformat()
        )
        self.requests[request_id] = req
        self._publish("policy.rollback_requested", req.__dict__)
        return req

    def approve_and_execute(self, request_id: str) -> PolicyVersion:
        req = self.requests[request_id]
        if req.executed:
            return self.governance.active
        if not self.governance.history:
            raise RuntimeError("no previous policy available for rollback")
        restored = self.governance.rollback()
        self.registry.activate(restored)
        self.kernel.reload_active_policy()
        self.requests[request_id] = RollbackRequest(
            req.request_id, req.policy_version, req.reason, req.created_at, True, True
        )
        self._publish(
            "policy.rollback_executed",
            {
                **self.requests[request_id].__dict__,
                "restored_version": restored.version,
            },
        )
        return restored

    def _publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.event_bus is not None and hasattr(self.event_bus, "publish"):
            self.event_bus.publish(event_type, payload)
