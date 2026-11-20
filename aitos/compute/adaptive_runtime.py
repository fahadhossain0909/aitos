"""Safe assembly of opt-in adaptive compute primitives from validated tuning."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aitos.compute.pressure_controller import (
    PressurePolicy,
    ProcessPressureSampler,
    ResourcePressureController,
)
from aitos.compute.work_scheduler import (
    WorkloadAdmission,
    WorkloadRequest,
    admit_optional_work,
)
from aitos.compute.resource_governor import (
    ResourceBudget,
    ResourceSnapshot,
    build_resource_budget,
    detect_resources,
)

if TYPE_CHECKING:
    from aitos.config.tuning import TuningSettings


@dataclass(frozen=True)
class AdaptiveComputeRuntime:
    """Immutable startup snapshot plus advisory pressure state."""

    resources: ResourceSnapshot
    budget: ResourceBudget
    pressure_controller: ResourcePressureController
    pressure_sampler: ProcessPressureSampler

    def update_pressure(
        self,
        *,
        now_seconds: float,
        cpu_utilization_pct: float,
        memory_utilization_pct: float,
        queue_lag_seconds: float | None = None,
    ):
        """Update hysteretic pressure state from host/service telemetry."""
        return self.pressure_controller.update(
            now_seconds=now_seconds,
            cpu_utilization_pct=cpu_utilization_pct,
            memory_utilization_pct=memory_utilization_pct,
            queue_lag_seconds=queue_lag_seconds,
        )

    def observe_process_pressure(self, *, queue_lag_seconds: float | None = None):
        """Sample host process pressure and update the shared hysteresis state."""
        sample = self.pressure_sampler.sample(queue_lag_seconds=queue_lag_seconds)
        return self.update_pressure(
            now_seconds=sample.observed_at,
            cpu_utilization_pct=sample.cpu_utilization_pct,
            memory_utilization_pct=sample.memory_utilization_pct,
            queue_lag_seconds=sample.queue_lag_seconds,
        )

    def budget_for_optional_work(self) -> ResourceBudget:
        """Return a pressure-scaled budget for optional scanner/analysis work."""
        level = int(self.pressure_controller.level)
        scale = {0: 1.0, 1: 0.5, 2: 0.25, 3: 0.0}[level]

        def scaled_count(value: int) -> int:
            if scale <= 0 or value <= 0:
                return 0
            return max(1, int(value * scale))

        return replace(
            self.budget,
            cpu_workers=scaled_count(self.budget.cpu_workers),
            background_task_limit=scaled_count(self.budget.background_task_limit),
            deep_analysis_slots=scaled_count(self.budget.deep_analysis_slots),
            memory_budget_mb=int(self.budget.memory_budget_mb * scale),
            cpu_capacity_units=self.budget.cpu_capacity_units * scale,
        )

    def admit_optional_work(
        self, requests: list[WorkloadRequest] | tuple[WorkloadRequest, ...]
    ) -> WorkloadAdmission:
        """Admit optional work against the latest pressure state and hard budget."""
        return admit_optional_work(
            requests,
            cpu_capacity_units=self.budget.cpu_capacity_units,
            memory_capacity_mb=self.budget.memory_budget_mb,
            pressure_level=int(self.pressure_controller.level),
        )


def create_adaptive_compute_runtime(
    settings: TuningSettings,
    *,
    resources: ResourceSnapshot | None = None,
) -> AdaptiveComputeRuntime | None:
    """Create compute controls only when explicitly enabled for paper mode.

    Hardware tuning values are upper bounds, never resource guarantees. The
    detected process/cgroup limits remain authoritative. Live mode fails
    closed even if a caller bypasses normal Pydantic settings validation.
    """
    if settings.mode == "live":
        if settings.runtime.adaptive_compute_enabled:
            raise ValueError("adaptive compute runtime is paper-mode only")
        return None
    if not settings.runtime.adaptive_compute_enabled:
        return None

    detected = resources or detect_resources()
    constrained = replace(
        detected,
        memory_limit_mb=min(
            detected.memory_limit_mb, settings.hardware.memory_budget_mb
        ),
        memory_available_mb=min(
            detected.memory_available_mb, settings.hardware.memory_budget_mb
        ),
    )
    budget = build_resource_budget(
        constrained,
        memory_reserve_pct=settings.hardware.memory_reserve_pct,
        max_workers=settings.hardware.cpu_worker_budget,
        max_background_tasks=settings.hardware.max_background_tasks,
    )
    policy = PressurePolicy(
        warning_hold_seconds=settings.runtime.pressure_warning_hold_seconds,
        high_hold_seconds=settings.runtime.pressure_high_hold_seconds,
        recovery_hold_seconds=settings.runtime.pressure_recovery_hold_seconds,
        minimum_transition_interval_seconds=(
            settings.runtime.pressure_minimum_transition_interval_seconds
        ),
    )
    return AdaptiveComputeRuntime(
        resources=constrained,
        budget=budget,
        pressure_controller=ResourcePressureController(policy),
        pressure_sampler=ProcessPressureSampler(constrained),
    )
