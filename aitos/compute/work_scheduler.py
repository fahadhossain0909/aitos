"""Pressure-aware admission for optional analysis and background work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from math import isfinite


class WorkloadClass(IntEnum):
    INTERACTIVE = 0
    BACKGROUND = 1
    BULK = 2


@dataclass(frozen=True)
class WorkloadRequest:
    request_id: str
    workload_class: WorkloadClass
    cpu_cost_units: float
    memory_cost_mb: int = 0
    priority: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id cannot be empty")
        if not isinstance(self.workload_class, WorkloadClass):
            raise ValueError("workload_class must be a WorkloadClass")
        if (
            not isfinite(self.cpu_cost_units)
            or not isfinite(self.priority)
            or self.cpu_cost_units < 0
            or self.priority < 0
            or isinstance(self.memory_cost_mb, bool)
            or not isinstance(self.memory_cost_mb, int)
            or self.memory_cost_mb < 0
        ):
            raise ValueError(
                "workload request costs and priority must be finite/non-negative"
            )


@dataclass(frozen=True)
class WorkloadAdmission:
    admitted: tuple[str, ...]
    deferred: tuple[str, ...]
    cpu_reserved_units: float
    memory_reserved_mb: int


def admit_optional_work(
    requests: list[WorkloadRequest] | tuple[WorkloadRequest, ...],
    *,
    cpu_capacity_units: float,
    memory_capacity_mb: int,
    pressure_level: int = 0,
    max_admitted: int | None = None,
) -> WorkloadAdmission:
    """Admit optional work only; protected trading services are out of scope.

    Pressure levels: NORMAL=0, WARNING=1, HIGH=2, CRITICAL=3. WARNING halves
    optional capacity, HIGH leaves a quarter-capacity interactive lane, and
    CRITICAL defers all optional requests. Protected market-data, order,
    position, risk, and execution services require separate capacity ownership.
    """
    if (
        isinstance(cpu_capacity_units, bool)
        or not isinstance(cpu_capacity_units, (int, float))
        or not isfinite(cpu_capacity_units)
        or cpu_capacity_units < 0
        or isinstance(memory_capacity_mb, bool)
        or not isinstance(memory_capacity_mb, int)
        or memory_capacity_mb < 0
        or isinstance(pressure_level, bool)
        or not isinstance(pressure_level, int)
        or pressure_level not in (0, 1, 2, 3)
        or (
            max_admitted is not None
            and (
                isinstance(max_admitted, bool)
                or not isinstance(max_admitted, int)
                or max_admitted < 0
            )
        )
    ):
        raise ValueError("invalid optional-work admission limits")

    if not isinstance(requests, (list, tuple)):
        raise ValueError("requests must be a list or tuple")

    seen: set[str] = set()
    for request in requests:
        if not isinstance(request, WorkloadRequest):
            raise ValueError("requests must contain WorkloadRequest values")
        key = request.request_id.strip()
        if key in seen:
            raise ValueError(f"duplicate workload request_id: {key}")
        seen.add(key)

    cpu_limit = cpu_capacity_units
    memory_limit = memory_capacity_mb
    if pressure_level == 1:
        cpu_limit *= 0.5
        memory_limit //= 2
    elif pressure_level == 2:
        cpu_limit *= 0.25
        memory_limit //= 4
    elif pressure_level == 3:
        cpu_limit = 0.0
        memory_limit = 0

    ordered = sorted(
        requests,
        key=lambda item: (int(item.workload_class), -item.priority, item.request_id),
    )
    admitted: list[str] = []
    deferred: list[str] = []
    cpu_used = 0.0
    memory_used = 0
    for request in ordered:
        within_count = max_admitted is None or len(admitted) < max_admitted
        within_budget = (
            cpu_used + request.cpu_cost_units <= cpu_limit
            and memory_used + request.memory_cost_mb <= memory_limit
        )
        pressure_allows = not (
            pressure_level == 3
            or (
                pressure_level == 2
                and request.workload_class != WorkloadClass.INTERACTIVE
            )
        )
        if within_count and within_budget and pressure_allows:
            admitted.append(request.request_id)
            cpu_used += request.cpu_cost_units
            memory_used += request.memory_cost_mb
        else:
            deferred.append(request.request_id)

    return WorkloadAdmission(
        admitted=tuple(admitted),
        deferred=tuple(deferred),
        cpu_reserved_units=cpu_used,
        memory_reserved_mb=memory_used,
    )
