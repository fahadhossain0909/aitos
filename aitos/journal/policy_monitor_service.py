"""Event-driven policy monitoring service.

Consumes attributed trade outcomes and emits governance recommendations.
It never promotes or rolls back a policy itself.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict

from aitos.core.contracts import (AITOSModule, Event, EventResponse,
                                  HealthStatus, ModuleStatus)
from aitos.eventbus.redis_bus import EventBus, Subscription
from aitos.journal.policy_monitor import evaluate_policy_health

TOPIC_ROLLBACK_RECOMMENDED = "policy.rollback_recommended"


class PolicyMonitorService(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        active_version: str = "baseline",
        baseline_avg_r: float = 0.0,
        window_size: int = 100,
        min_observations: int = 30,
        max_degradation: float = 0.20,
        min_avg_r: float = 0.0,
    ) -> None:
        self._event_bus = event_bus
        self._active_version = active_version
        self._baseline_avg_r = baseline_avg_r
        self._window = deque(maxlen=max(1, window_size))
        self._min_observations = min_observations
        self._max_degradation = max_degradation
        self._min_avg_r = min_avg_r
        self._subscription: Subscription | None = None
        self._initialized = False
        self._last_health: Dict[str, Any] | None = None

    @property
    def module_id(self) -> str:
        return "policy-monitor-service"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: Dict[str, Any]) -> None:
        if self._initialized:
            return
        self._subscription = await self._event_bus.subscribe(
            "journal.outcome_attributed", self.handle_event, group="policy-monitor"
        )
        self._initialized = True

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            module_id=self.module_id,
            status=(
                ModuleStatus.HEALTHY if self._initialized else ModuleStatus.UNHEALTHY
            ),
            latency_ms=0.0,
            last_event_time=None,
            details={
                "active_version": self._active_version,
                "window_size": len(self._window),
                "last_health": self._last_health,
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        if self._subscription:
            self._subscription.cancel()
            self._subscription = None
        self._initialized = False

    async def emit_events(self):
        if False:
            yield None

    async def handle_event(self, event: Event) -> EventResponse | None:
        payload = dict(event.payload)
        r = payload.get("r_multiple")
        if not isinstance(r, (int, float)):
            return None
        self._window.append(
            {
                "r_multiple": float(r),
                "decision_id": payload.get("decision_id"),
                "trade_id": payload.get("trade_id"),
            }
        )
        health = evaluate_policy_health(
            self._active_version,
            list(self._window),
            baseline_avg_r=self._baseline_avg_r,
            min_observations=self._min_observations,
            max_degradation=self._max_degradation,
            min_avg_r=self._min_avg_r,
        )
        self._last_health = health.to_dict()
        if health.rollback_recommended:
            await self._event_bus.publish(
                Event(
                    topic=TOPIC_ROLLBACK_RECOMMENDED,
                    payload=health.to_dict(),
                    source_module=self.module_id,
                )
            )
        return None

    def set_active_policy(self, version: str, baseline_avg_r: float) -> None:
        self._active_version = version
        self._baseline_avg_r = float(baseline_avg_r)
        self._window.clear()
        self._last_health = None
