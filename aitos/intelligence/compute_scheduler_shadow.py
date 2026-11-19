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

    def observe(self, task: AnalysisTask, *, now: float) -> ShadowDecision:
        self._sequence += 1
        observed = AnalysisTask(
            task_id=f"shadow:{self._sequence}:{task.task_id}",
            priority=task.priority,
            cost_units=task.cost_units,
            submitted_at=task.submitted_at,
            payload=task.payload,
            coalesce_key=f"{task.coalesce_key}:{task.priority.name}" if task.coalesce_key else None,
            replaceable=task.replaceable,
        )
        # Invalid/overloaded policy admission is telemetry-only; never bubbles
        # into or suppresses the caller's authoritative execution path.
        try:
            self.scheduler.submit(observed, now=now)
        except ValueError:
            pass
        dispatched = self.scheduler.drain(now=now)
        decision = ShadowDecision(
            observed_task_ids=(task.task_id,),
            proposed_dispatch_ids=tuple(item.task_id for item in dispatched),
            snapshot=self.scheduler.snapshot(),
        )
        if self.observer:
            self.observer(decision)
        return decision
