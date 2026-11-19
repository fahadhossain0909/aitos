import math

import pytest

from aitos.intelligence.compute_scheduler import (
    AnalysisTask,
    ComputeAwareScheduler,
    TaskPriority,
)


def test_task_rejects_invalid_priority_and_non_finite_time():
    with pytest.raises(ValueError):
        AnalysisTask("bad", 99, submitted_at=0)
    with pytest.raises(ValueError):
        AnalysisTask("nan", TaskPriority.CANDIDATE_ANALYSIS, submitted_at=math.nan)


def test_scheduler_rejects_non_finite_clock():
    scheduler = ComputeAwareScheduler()
    task = AnalysisTask("x", TaskPriority.CANDIDATE_ANALYSIS, submitted_at=0)
    with pytest.raises(ValueError):
        scheduler.submit(task, now=math.inf)
    with pytest.raises(ValueError):
        scheduler.drain(now=math.nan)
