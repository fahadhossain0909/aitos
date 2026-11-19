from aitos.intelligence.compute_scheduler import (
    AnalysisTask,
    ComputeAwareScheduler,
    TaskPriority,
)


def task(task_id, priority, *, cost=1, at=0, key=None, replaceable=False):
    return AnalysisTask(
        task_id=task_id,
        priority=priority,
        cost_units=cost,
        submitted_at=at,
        coalesce_key=key,
        replaceable=replaceable,
    )


def test_position_safety_reserve_is_not_consumed_by_candidates():
    scheduler = ComputeAwareScheduler(
        capacity_units=10, position_reserve_units=4, market_reserve_units=2
    )
    scheduler.submit(task("candidate", TaskPriority.CANDIDATE_ANALYSIS, cost=8), now=1)
    scheduler.submit(task("position", TaskPriority.POSITION_SAFETY, cost=4), now=1)
    selected = scheduler.drain(now=1)
    assert [item.task_id for item in selected] == ["position"]
    assert scheduler.snapshot().queued == 1


def test_market_critical_reserve_protects_critical_work():
    scheduler = ComputeAwareScheduler(
        capacity_units=10, position_reserve_units=2, market_reserve_units=4
    )
    scheduler.submit(task("research", TaskPriority.BACKGROUND_RESEARCH, cost=7), now=1)
    scheduler.submit(task("market", TaskPriority.MARKET_CRITICAL, cost=4), now=1)
    assert [item.task_id for item in scheduler.drain(now=1)] == ["market"]


def test_replaceable_tasks_coalesce_but_canonical_work_cannot():
    scheduler = ComputeAwareScheduler()
    scheduler.submit(
        task("old", TaskPriority.CANDIDATE_ANALYSIS, at=1, key="BTC", replaceable=True),
        now=1,
    )
    scheduler.submit(
        task("new", TaskPriority.CANDIDATE_ANALYSIS, at=2, key="BTC", replaceable=True),
        now=2,
    )
    assert scheduler.snapshot().queued == 1
    assert scheduler.snapshot().coalesced == 1
    assert scheduler.drain(now=2)[0].task_id == "new"


def test_lower_priority_cannot_replace_urgent_coalesced_task():
    scheduler = ComputeAwareScheduler()
    scheduler.submit(
        task("urgent", TaskPriority.POSITION_SAFETY, key="position:1", replaceable=True),
        now=1,
    )
    accepted = scheduler.submit(
        task("background", TaskPriority.BACKGROUND_RESEARCH, key="position:1", replaceable=True),
        now=2,
    )
    assert not accepted
    assert scheduler.drain(now=2)[0].task_id == "urgent"


def test_bounded_queue_evicts_only_replaceable_lower_priority_work():
    scheduler = ComputeAwareScheduler(max_pending=1)
    scheduler.submit(
        task("research", TaskPriority.BACKGROUND_RESEARCH, key="research:1", replaceable=True),
        now=1,
    )
    assert scheduler.submit(task("position", TaskPriority.POSITION_SAFETY), now=2)
    assert scheduler.snapshot().evicted == 1
    assert scheduler.drain(now=2)[0].task_id == "position"


def test_full_queue_rejects_instead_of_dropping_nonreplaceable_work():
    scheduler = ComputeAwareScheduler(max_pending=1)
    scheduler.submit(task("market", TaskPriority.MARKET_CRITICAL), now=1)
    assert not scheduler.submit(task("position", TaskPriority.POSITION_SAFETY), now=2)
    assert scheduler.snapshot().rejected == 1


def test_drain_respects_compute_budget_and_task_limit():
    scheduler = ComputeAwareScheduler(capacity_units=5)
    for i in range(4):
        scheduler.submit(task(str(i), TaskPriority.CANDIDATE_ANALYSIS, cost=2), now=1)
    selected = scheduler.drain(now=1, max_tasks=2)
    assert len(selected) == 2
    assert sum(item.cost_units for item in selected) == 4
    assert scheduler.snapshot().queued == 2


def test_validation_rejects_invalid_reserves_and_unsafe_coalescing():
    try:
        ComputeAwareScheduler(capacity_units=4, position_reserve_units=3, market_reserve_units=2)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid reserve configuration accepted")
    try:
        task("raw-depth", TaskPriority.MARKET_CRITICAL, key="BTC")
    except ValueError:
        pass
    else:
        raise AssertionError("non-replaceable task was allowed to coalesce")


def test_aged_background_work_can_compete_with_candidates():
    scheduler = ComputeAwareScheduler(capacity_units=1, aging_seconds=10)
    scheduler.submit(
        task("research", TaskPriority.BACKGROUND_RESEARCH, at=0), now=0
    )
    scheduler.submit(
        task("candidate", TaskPriority.CANDIDATE_ANALYSIS, at=100), now=100
    )
    assert scheduler.drain(now=100)[0].task_id == "research"


def test_pending_task_ids_must_be_unique():
    scheduler = ComputeAwareScheduler()
    scheduler.submit(task("same", TaskPriority.CANDIDATE_ANALYSIS), now=0)
    try:
        scheduler.submit(task("same", TaskPriority.MARKET_CRITICAL), now=0)
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate pending task id was accepted")
