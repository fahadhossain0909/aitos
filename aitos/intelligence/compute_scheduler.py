"""Bounded, compute-aware scheduling for replaceable intelligence work.

This scheduler is downstream of canonical market-data ingestion. It must never
own, drop, reorder, or coalesce raw exchange events or order-book deltas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class TaskPriority(IntEnum):
    POSITION_SAFETY = 0
    MARKET_CRITICAL = 1
    CANDIDATE_ANALYSIS = 2
    BACKGROUND_RESEARCH = 3


@dataclass(frozen=True)
class AnalysisTask:
    task_id: str
    priority: TaskPriority
    cost_units: int = 1
    submitted_at: float = 0.0
    payload: Any = None
    coalesce_key: str | None = None
    replaceable: bool = False

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if not isinstance(self.priority, TaskPriority):
            raise ValueError("priority must be a TaskPriority")
        if not math.isfinite(self.submitted_at):
            raise ValueError("submitted_at must be finite")
        if self.cost_units < 1:
            raise ValueError("cost_units must be positive")
        if self.coalesce_key and not self.replaceable:
            raise ValueError("only replaceable derived tasks may be coalesced")


@dataclass(frozen=True)
class SchedulerSnapshot:
    queued: int
    accepted: int
    rejected: int
    coalesced: int
    evicted: int
    dispatched: int
    pending_by_priority: dict[str, int]


class ComputeAwareScheduler:
    """Deterministic bounded scheduler with protected urgent-work capacity.

    capacity_units is the maximum estimated compute cost dispatched in one
    drain cycle. Reserved units are protected from lower-priority work only
    while a task at that priority (or higher) is waiting. Cost is an admission
    estimate, not a CPU-time guarantee; callers should calibrate it from
    observed stage timings.
    """

    def __init__(
        self,
        *,
        max_pending: int = 1024,
        capacity_units: int = 32,
        position_reserve_units: int | None = None,
        market_reserve_units: int | None = None,
        aging_seconds: float = 30.0,
    ) -> None:
        if max_pending < 1 or capacity_units < 1:
            raise ValueError("queue and capacity must be positive")
        # Scale implicit reserves for small test/dev budgets while retaining
        # the established 8+8 reservation at the default 32-unit capacity.
        default_reserve = min(8, capacity_units // 4)
        position_reserve_units = (
            default_reserve
            if position_reserve_units is None
            else position_reserve_units
        )
        market_reserve_units = (
            default_reserve if market_reserve_units is None else market_reserve_units
        )
        if min(position_reserve_units, market_reserve_units) < 0:
            raise ValueError("reserved capacity cannot be negative")
        if position_reserve_units + market_reserve_units > capacity_units:
            raise ValueError("reserved capacity exceeds total capacity")
        if aging_seconds <= 0:
            raise ValueError("aging_seconds must be positive")
        self.max_pending = max_pending
        self.capacity_units = capacity_units
        self.position_reserve_units = position_reserve_units
        self.market_reserve_units = market_reserve_units
        self.aging_seconds = aging_seconds
        self._pending: list[AnalysisTask] = []
        self._accepted = self._rejected = self._coalesced = 0
        self._evicted = self._dispatched = 0

    def submit(self, task: AnalysisTask, *, now: float) -> bool:
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if task.submitted_at > now:
            raise ValueError("submitted_at cannot be in the future")
        if task.cost_units > self.capacity_units:
            raise ValueError("task cost exceeds one-cycle compute capacity")
        if any(item.task_id == task.task_id for item in self._pending):
            raise ValueError("task_id must be unique while a task is pending")
        if task.coalesce_key:
            for index, current in enumerate(self._pending):
                if current.coalesce_key == task.coalesce_key:
                    if task.priority > current.priority:
                        self._rejected += 1
                        return False
                    self._pending[index] = task
                    self._coalesced += 1
                    return True
        if len(self._pending) >= self.max_pending:
            lower = [
                (i, queued)
                for i, queued in enumerate(self._pending)
                if queued.priority > task.priority and queued.replaceable
            ]
            if not lower:
                self._rejected += 1
                return False
            index, _ = max(
                lower, key=lambda item: (item[1].priority, -item[1].submitted_at)
            )
            self._pending.pop(index)
            self._evicted += 1
        self._pending.append(task)
        self._accepted += 1
        return True

    def drain(self, *, now: float, max_tasks: int | None = None) -> list[AnalysisTask]:
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if max_tasks is not None and max_tasks < 0:
            raise ValueError("max_tasks cannot be negative")
        remaining = self.capacity_units
        dispatched: list[AnalysisTask] = []
        pending = list(self._pending)

        while pending and remaining > 0:
            urgent_waiting = any(
                item.priority == TaskPriority.POSITION_SAFETY for item in pending
            )
            market_waiting = any(
                item.priority == TaskPriority.MARKET_CRITICAL for item in pending
            )

            def effective_priority(item: AnalysisTask) -> int:
                # Position and market-critical work never age downward.
                if item.priority <= TaskPriority.MARKET_CRITICAL:
                    return int(item.priority)
                age_steps = int(max(0.0, now - item.submitted_at) / self.aging_seconds)
                return max(
                    int(TaskPriority.CANDIDATE_ANALYSIS),
                    int(item.priority) - age_steps,
                )

            ranked = sorted(
                pending,
                key=lambda item: (
                    effective_priority(item),
                    item.submitted_at,
                    item.task_id,
                ),
            )
            chosen = None
            for item in ranked:
                if max_tasks is not None and len(dispatched) >= max_tasks:
                    break
                reserve = 0
                if item.priority > TaskPriority.POSITION_SAFETY and urgent_waiting:
                    reserve += self.position_reserve_units
                if item.priority > TaskPriority.MARKET_CRITICAL and market_waiting:
                    reserve += self.market_reserve_units
                if item.cost_units <= remaining - reserve:
                    chosen = item
                    break
            if chosen is None:
                break
            pending.remove(chosen)
            dispatched.append(chosen)
            remaining -= chosen.cost_units

        dispatched_ids = {item.task_id for item in dispatched}
        self._pending = [
            item for item in self._pending if item.task_id not in dispatched_ids
        ]
        self._dispatched += len(dispatched)
        return dispatched

    def snapshot(self) -> SchedulerSnapshot:
        counts = {
            priority.name.lower(): sum(
                item.priority == priority for item in self._pending
            )
            for priority in TaskPriority
        }
        return SchedulerSnapshot(
            queued=len(self._pending),
            accepted=self._accepted,
            rejected=self._rejected,
            coalesced=self._coalesced,
            evicted=self._evicted,
            dispatched=self._dispatched,
            pending_by_priority=counts,
        )
