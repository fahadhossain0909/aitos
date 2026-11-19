"""Safe assembly of opt-in adaptive compute primitives from validated tuning."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aitos.compute.pressure_controller import (
    PressurePolicy,
    ResourcePressureController,
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
    )
