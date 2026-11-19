from aitos.intelligence.compute_scheduler import AnalysisTask, TaskPriority
from aitos.intelligence.compute_scheduler_shadow import ComputeSchedulerShadow

def test_shadow_observes_without_mutating_caller_task():
    seen = []
    shadow = ComputeSchedulerShadow(observer=seen.append)
    task = AnalysisTask("position-1", TaskPriority.POSITION_SAFETY, submitted_at=1)
    result = shadow.observe(task, now=1)
    assert task.task_id == "position-1"
    assert result.proposed_dispatch_ids == ("shadow:1:position-1",)
    assert seen == [result]

def test_shadow_namespaces_reused_ids():
    shadow = ComputeSchedulerShadow()
    task = AnalysisTask("BTC", TaskPriority.CANDIDATE_ANALYSIS, submitted_at=1)
    assert shadow.observe(task, now=1).proposed_dispatch_ids == ("shadow:1:BTC",)
    assert shadow.observe(task, now=2).proposed_dispatch_ids == ("shadow:2:BTC",)

def test_shadow_invalid_admission_does_not_raise_to_caller():
    shadow = ComputeSchedulerShadow()
    task = AnalysisTask("large", TaskPriority.BACKGROUND_RESEARCH, cost_units=40, submitted_at=1)
    result = shadow.observe(task, now=1)
    assert result.proposed_dispatch_ids == ()
