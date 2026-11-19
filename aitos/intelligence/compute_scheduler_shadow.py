"""Read-only shadow adapter for compute-aware task scheduling."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from aitos.intelligence.compute_scheduler import AnalysisTask, ComputeAwareScheduler, SchedulerSnapshot

@dataclass(frozen=True)
class ShadowDecision:
    observed_task_ids: tuple[str, ...]
    proposed_dispatch_ids: tuple[str, ...]
    snapshot: SchedulerSnapshot

class ComputeSchedulerShadow:
    """Observe policy decisions without controlling existing execution."""
    def __init__(self, scheduler=None, observer: Callable[[ShadowDecision], None] | None = None):
        self.scheduler = scheduler or ComputeAwareScheduler()
        self.observer = observer
        self._sequence = 0
        self._observed: list[str] = []

    def observe(self, task: AnalysisTask, *, now: float) -> ShadowDecision:
        """Queue an intent for shadow comparison; caller still executes normally."""
        self._sequence += 1
        observed = AnalysisTask(
            task_id=f"shadow:{self._sequence}:{task.task_id}",
            priority=task.priority, cost_units=task.cost_units,
            submitted_at=task.submitted_at, payload=task.payload,
            coalesce_key=task.coalesce_key, replaceable=task.replaceable,
        )
        self._observed.append(task.task_id)
        try:
            self.scheduler.submit(observed, now=now)
        except ValueError:
            # Invalid/over-budget policy admission is telemetry-only in shadow.
            pass
        decision = ShadowDecision(
            observed_task_ids=(task.task_id,), proposed_dispatch_ids=(),
            snapshot=self.scheduler.snapshot(),
        )
        if self.observer:
            self.observer(decision)
        return decision

    def flush(self, *, now: float, max_tasks: int | None = None) -> ShadowDecision:
        """Evaluate one scheduling cycle without controlling production work."""
        dispatched = self.scheduler.drain(now=now, max_tasks=max_tasks)
        decision = ShadowDecision(
            observed_task_ids=tuple(self._observed),
            proposed_dispatch_ids=tuple(item.task_id for item in dispatched),
            snapshot=self.scheduler.snapshot(),
        )
        self._observed.clear()
        if self.observer:
            self.observer(decision)
        return decision
