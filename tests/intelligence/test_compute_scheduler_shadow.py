from aitos.intelligence.compute_scheduler import (
    AnalysisTask,
    ComputeAwareScheduler,
    TaskPriority,
)
from aitos.intelligence.compute_scheduler_shadow import ComputeSchedulerShadow


def test_shadow_batches_observations_without_mutating_caller_task():
    seen = []
    scheduler = ComputeAwareScheduler(
        capacity_units=2, position_reserve_units=0, market_reserve_units=0
    )
    shadow = ComputeSchedulerShadow(scheduler=scheduler, observer=seen.append)
    task = AnalysisTask("position-1", TaskPriority.POSITION_SAFETY, submitted_at=1)
    observed = shadow.observe(task, now=1)
    assert task.task_id == "position-1"
    assert observed.proposed_dispatch_ids == ()
    result = shadow.flush(now=1)
    assert result.proposed_dispatch_ids == ("shadow:1:position-1",)
    assert seen[-1] == result


def test_shadow_namespaces_reused_ids_across_cycles():
    shadow = ComputeSchedulerShadow()
    task = AnalysisTask("BTC", TaskPriority.CANDIDATE_ANALYSIS, submitted_at=1)
    shadow.observe(task, now=1)
    assert shadow.flush(now=1).proposed_dispatch_ids == ("shadow:1:BTC",)
    shadow.observe(task, now=2)
    assert shadow.flush(now=2).proposed_dispatch_ids == ("shadow:2:BTC",)


def test_shadow_invalid_admission_does_not_raise_to_caller():
    shadow = ComputeSchedulerShadow()
    task = AnalysisTask(
        "large", TaskPriority.BACKGROUND_RESEARCH, cost_units=40, submitted_at=1
    )
    result = shadow.observe(task, now=1)
    assert result.proposed_dispatch_ids == ()
    assert result.snapshot.queued == 0
