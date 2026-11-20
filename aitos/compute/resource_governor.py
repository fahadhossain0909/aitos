"""Hardware-aware, risk-neutral compute capacity planning.

This module deliberately does not change strategy, execution, or financial risk.
It discovers the resources visible to the current process (including Linux
cgroup limits) and turns them into conservative workload-admission hints.
"""

from __future__ import annotations

import glob
import os
from math import isfinite
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ResourceSnapshot:
    logical_cpus: int
    effective_cpus: float
    memory_total_mb: int
    memory_limit_mb: int
    memory_available_mb: int
    cpu_quota_limited: bool
    memory_limit_limited: bool
    accelerators: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.logical_cpus, bool)
            or not isinstance(self.logical_cpus, int)
            or self.logical_cpus < 1
            or isinstance(self.effective_cpus, bool)
            or not isinstance(self.effective_cpus, (int, float))
            or not isfinite(self.effective_cpus)
            or self.effective_cpus <= 0
        ):
            raise ValueError("CPU capacity must be finite and positive")
        memory_values = (
            self.memory_total_mb,
            self.memory_limit_mb,
            self.memory_available_mb,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in memory_values
        ):
            raise ValueError("memory values cannot be negative")


@dataclass(frozen=True)
class ResourceBudget:
    cpu_workers: int
    memory_budget_mb: int
    background_task_limit: int
    deep_analysis_slots: int
    cpu_capacity_units: float

    def __post_init__(self) -> None:
        integer_limits = (
            self.cpu_workers,
            self.memory_budget_mb,
            self.background_task_limit,
            self.deep_analysis_slots,
        )
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in integer_limits
            )
            or self.cpu_workers < 1
        ):
            raise ValueError("resource budget limits must be non-negative integers")
        if (
            isinstance(self.cpu_capacity_units, bool)
            or not isinstance(self.cpu_capacity_units, (int, float))
            or not isfinite(self.cpu_capacity_units)
            or self.cpu_capacity_units < 0
        ):
            raise ValueError("CPU capacity units must be finite and non-negative")


@dataclass(frozen=True)
class SymbolWorkload:
    symbol: str
    cpu_cost_units: float = 10.0
    memory_cost_mb: int = 32
    opportunity_score: float = 0.0
    liquidity_score: float = 0.0
    mandatory: bool = False
    open_position: bool = False
    pending_order: bool = False

    @property
    def protected(self) -> bool:
        return self.mandatory or self.open_position or self.pending_order

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol cannot be empty")
        if (
            isinstance(self.cpu_cost_units, bool)
            or not isinstance(self.cpu_cost_units, (int, float))
            or isinstance(self.opportunity_score, bool)
            or not isinstance(self.opportunity_score, (int, float))
            or isinstance(self.liquidity_score, bool)
            or not isinstance(self.liquidity_score, (int, float))
            or not isfinite(self.cpu_cost_units)
            or not isfinite(self.opportunity_score)
            or not isfinite(self.liquidity_score)
            or self.cpu_cost_units < 0
            or isinstance(self.memory_cost_mb, bool)
            or not isinstance(self.memory_cost_mb, int)
            or self.memory_cost_mb < 0
        ):
            raise ValueError(
                "workload costs and scores must be finite and costs non-negative"
            )


@dataclass(frozen=True)
class CapacityPlan:
    selected_symbols: tuple[str, ...]
    deferred_symbols: tuple[str, ...]
    protected_symbols: tuple[str, ...]
    deep_analysis_limit: int
    budget: ResourceBudget
    overcommitted_by_protected_work: bool


class PressureLevel(IntEnum):
    NORMAL = 0
    WARNING = 1
    HIGH = 2
    CRITICAL = 3


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _memory_values(proc_root: Path, cgroup_root: Path) -> tuple[int, int, int, bool]:
    total_kb = 0
    available_kb = 0
    meminfo = _read_text(proc_root / "meminfo")
    if meminfo:
        values: dict[str, int] = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                values[parts[0].rstrip(":")] = int(parts[1])
        total_kb = values.get("MemTotal", 0)
        available_kb = values.get("MemAvailable", values.get("MemFree", 0))

    limit_bytes: int | None = None
    current_bytes: int | None = None
    for relative in (
        "memory.max",
        "memory/memory.limit_in_bytes",
        "memory.limit_in_bytes",
    ):
        value = _read_text(cgroup_root / relative)
        if value and value != "max":
            try:
                parsed = int(value)
                if 0 < parsed < (1 << 60):
                    limit_bytes = (
                        parsed if limit_bytes is None else min(limit_bytes, parsed)
                    )
            except ValueError:
                pass
    for relative in (
        "memory.current",
        "memory/memory.usage_in_bytes",
        "memory.usage_in_bytes",
    ):
        value = _read_text(cgroup_root / relative)
        if value:
            try:
                current_bytes = int(value)
                break
            except ValueError:
                pass

    host_total = total_kb // 1024
    host_available = available_kb // 1024
    if limit_bytes is None:
        limit_mb = host_total
        available_mb = host_available
        limited = False
    else:
        limit_mb = limit_bytes // (1024 * 1024)
        cgroup_available = max(0, limit_mb - ((current_bytes or 0) // (1024 * 1024)))
        available_mb = (
            min(host_available, cgroup_available)
            if host_available
            else cgroup_available
        )
        if host_total:
            limit_mb = min(limit_mb, host_total)
        limited = bool(host_total and limit_mb < host_total)
    return host_total, limit_mb, available_mb, limited


def _cpu_values(proc_root: Path, cgroup_root: Path) -> tuple[int, float, bool]:
    logical = max(1, os.cpu_count() or 1)
    try:
        affinity = len(os.sched_getaffinity(0))
        if affinity:
            logical = min(logical, affinity)
    except (AttributeError, OSError):
        pass

    quota_cpus: float | None = None
    cpu_max = _read_text(cgroup_root / "cpu.max")
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    quota_cpus = quota / period
            except ValueError:
                pass
    if quota_cpus is None:
        quota = _read_text(cgroup_root / "cpu/cpu.cfs_quota_us") or _read_text(
            cgroup_root / "cpu.cfs_quota_us"
        )
        period = _read_text(cgroup_root / "cpu/cpu.cfs_period_us") or _read_text(
            cgroup_root / "cpu.cfs_period_us"
        )
        try:
            if quota and period and int(quota) > 0 and int(period) > 0:
                quota_cpus = int(quota) / int(period)
        except ValueError:
            pass

    effective = min(float(logical), quota_cpus) if quota_cpus else float(logical)
    return logical, max(0.1, effective), bool(quota_cpus and quota_cpus < logical)


def detect_resources(
    *,
    proc_root: str | Path = "/proc",
    cgroup_root: str | Path = "/sys/fs/cgroup",
    device_root: str | Path = "/dev",
) -> ResourceSnapshot:
    """Detect resources visible to this process; safely degrades on non-Linux."""
    proc = Path(proc_root)
    cgroup = Path(cgroup_root)
    logical, effective, cpu_limited = _cpu_values(proc, cgroup)
    total, limit, available, memory_limited = _memory_values(proc, cgroup)
    accelerators: list[str] = []
    devices = Path(device_root)
    if glob.glob(str(devices / "nvidia[0-9]*")) or (devices / "nvidiactl").exists():
        accelerators.append("cuda_device")
    if glob.glob(str(devices / "dri" / "renderD*")):
        accelerators.append("drm_render_device")
    return ResourceSnapshot(
        logical_cpus=logical,
        effective_cpus=effective,
        memory_total_mb=total,
        memory_limit_mb=limit,
        memory_available_mb=available,
        cpu_quota_limited=cpu_limited,
        memory_limit_limited=memory_limited,
        accelerators=tuple(accelerators),
    )


def build_resource_budget(
    resources: ResourceSnapshot,
    *,
    memory_reserve_pct: int = 25,
    cpu_utilization_target: float = 0.75,
    max_workers: int = 256,
    max_background_tasks: int = 4,
) -> ResourceBudget:
    """Build conservative compute budgets without modifying trading-risk limits."""
    if (
        isinstance(memory_reserve_pct, bool)
        or not isinstance(memory_reserve_pct, int)
        or not 5 <= memory_reserve_pct <= 80
    ):
        raise ValueError("memory_reserve_pct must be between 5 and 80")
    if (
        isinstance(cpu_utilization_target, bool)
        or not isinstance(cpu_utilization_target, (int, float))
        or not isfinite(cpu_utilization_target)
        or not 0.1 <= cpu_utilization_target <= 0.95
    ):
        raise ValueError("cpu_utilization_target must be between 0.1 and 0.95")
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or max_workers < 1
        or isinstance(max_background_tasks, bool)
        or not isinstance(max_background_tasks, int)
        or max_background_tasks < 0
    ):
        raise ValueError("worker limits are invalid")

    workers = min(max_workers, max(1, int(resources.effective_cpus)))
    usable_memory = max(
        0, resources.memory_limit_mb * (100 - memory_reserve_pct) // 100
    )
    # Do not budget more than currently available memory. A low available-memory
    # reading is treated conservatively rather than assuming reclaim will succeed.
    usable_memory = min(usable_memory, resources.memory_available_mb)
    return ResourceBudget(
        cpu_workers=workers,
        memory_budget_mb=usable_memory,
        background_task_limit=min(max_background_tasks, max(0, workers - 1)),
        deep_analysis_slots=(
            min(workers, workers * 2) if usable_memory > 0 else 0
        ),
        cpu_capacity_units=resources.effective_cpus * 100.0 * cpu_utilization_target,
    )


def plan_symbol_capacity(
    workloads: Iterable[SymbolWorkload],
    budget: ResourceBudget,
    *,
    max_symbols: int = 5000,
) -> CapacityPlan:
    """Admit highest-value workloads while never silently dropping protected symbols.

    Open positions and pending-order symbols are always retained. If their
    combined cost exceeds the budget the plan reports overcommit; the caller
    must preserve risk/market-data duties and suppress optional admissions.
    """
    if (
        isinstance(max_symbols, bool)
        or not isinstance(max_symbols, int)
        or max_symbols < 1
    ):
        raise ValueError("max_symbols must be a positive integer")
    unique: dict[str, SymbolWorkload] = {}
    for item in workloads:
        key = item.symbol.strip().upper()
        previous = unique.get(key)
        if previous is None:
            unique[key] = item
        elif item.protected and not previous.protected:
            unique[key] = item
        elif item.protected == previous.protected and (
            item.opportunity_score,
            item.liquidity_score,
        ) > (previous.opportunity_score, previous.liquidity_score):
            unique[key] = item

    ordered = sorted(
        unique.values(),
        key=lambda item: (
            not item.protected,
            -(item.opportunity_score + 0.25 * item.liquidity_score)
            / max(item.cpu_cost_units, 0.1),
            item.symbol,
        ),
    )
    protected = [item for item in ordered if item.protected]
    optional = [item for item in ordered if not item.protected]
    selected = list(protected)
    cpu_used = sum(item.cpu_cost_units for item in protected)
    memory_used = sum(item.memory_cost_mb for item in protected)
    overcommitted = (
        cpu_used > budget.cpu_capacity_units or memory_used > budget.memory_budget_mb
    )

    if not overcommitted:
        for item in optional:
            if len(selected) >= max_symbols:
                break
            if (
                cpu_used + item.cpu_cost_units <= budget.cpu_capacity_units
                and memory_used + item.memory_cost_mb <= budget.memory_budget_mb
            ):
                selected.append(item)
                cpu_used += item.cpu_cost_units
                memory_used += item.memory_cost_mb

    selected_ids = {item.symbol.strip().upper() for item in selected}
    deferred = [
        item.symbol
        for item in ordered
        if item.symbol.strip().upper() not in selected_ids
    ]
    selected = selected[:max_symbols] if len(protected) <= max_symbols else protected
    # Protected symbols can exceed the configured soft cap: safety beats capacity.
    deep_slots = min(budget.deep_analysis_slots, max(0, len(selected) - len(protected)))
    return CapacityPlan(
        selected_symbols=tuple(item.symbol for item in selected),
        deferred_symbols=tuple(deferred),
        protected_symbols=tuple(item.symbol for item in protected),
        deep_analysis_limit=deep_slots,
        budget=budget,
        overcommitted_by_protected_work=overcommitted,
    )


def classify_pressure(
    *,
    cpu_utilization_pct: float,
    memory_utilization_pct: float,
    queue_lag_seconds: float | None,
) -> PressureLevel:
    """Classify operational pressure; this is advisory and never changes risk."""
    values = (cpu_utilization_pct, memory_utilization_pct)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
        for value in values
    ):
        raise ValueError("pressure metrics must be finite and non-negative")
    if queue_lag_seconds is not None and (
        isinstance(queue_lag_seconds, bool)
        or not isinstance(queue_lag_seconds, (int, float))
        or not isfinite(queue_lag_seconds)
        or queue_lag_seconds < 0
    ):
        raise ValueError("queue lag must be finite and non-negative when provided")
    if (
        cpu_utilization_pct >= 98
        or memory_utilization_pct >= 95
        or (queue_lag_seconds is not None and queue_lag_seconds >= 10)
    ):
        return PressureLevel.CRITICAL
    if (
        cpu_utilization_pct >= 90
        or memory_utilization_pct >= 90
        or (queue_lag_seconds is not None and queue_lag_seconds >= 5)
    ):
        return PressureLevel.HIGH
    if (
        cpu_utilization_pct >= 80
        or memory_utilization_pct >= 80
        or (queue_lag_seconds is not None and queue_lag_seconds >= 2)
    ):
        return PressureLevel.WARNING
    return PressureLevel.NORMAL
