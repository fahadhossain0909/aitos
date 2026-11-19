"""Synthetic benchmark for AITOS derived-analysis scheduling policy.

This measures scheduler admission/dispatch overhead and fairness under synthetic
task mixes. It does not benchmark exchange ingestion, Rust L3 reconstruction,
or production CPU throughput.
"""

from __future__ import annotations

import argparse
import json
import time

from aitos.intelligence.compute_scheduler import (
    AnalysisTask,
    ComputeAwareScheduler,
    TaskPriority,
)


def run(tasks: int = 10000, queue_capacity: int = 2048, cycle_budget: int = 64) -> dict:
    if tasks < 1:
        raise ValueError("tasks must be positive")
    scheduler = ComputeAwareScheduler(
        max_pending=queue_capacity,
        capacity_units=cycle_budget,
        position_reserve_units=max(1, cycle_budget // 4),
        market_reserve_units=max(1, cycle_budget // 4),
    )
    priorities = list(TaskPriority)
    started = time.perf_counter_ns()
    accepted = 0
    for index in range(tasks):
        priority = (
            TaskPriority.POSITION_SAFETY if index % 97 == 0 else priorities[index % 4]
        )
        task = AnalysisTask(
            task_id=f"bench-{index}",
            priority=priority,
            cost_units=1 + (index % min(4, cycle_budget)),
            submitted_at=float(index),
            coalesce_key=(
                f"candidate:{index % 128}"
                if priority == TaskPriority.CANDIDATE_ANALYSIS
                else None
            ),
            replaceable=priority
            in {TaskPriority.CANDIDATE_ANALYSIS, TaskPriority.BACKGROUND_RESEARCH},
        )
        accepted += int(scheduler.submit(task, now=float(index)))
        if index % 64 == 63:
            scheduler.drain(now=float(index))
    dispatched = 0
    while scheduler.snapshot().queued:
        batch = scheduler.drain(now=float(tasks + dispatched))
        if not batch:
            break
        dispatched += len(batch)
    elapsed = max(1, time.perf_counter_ns() - started)
    snapshot = scheduler.snapshot()
    return {
        "tasks_submitted": tasks,
        "accepted": accepted,
        "dispatched_total": snapshot.dispatched,
        "rejected": snapshot.rejected,
        "coalesced": snapshot.coalesced,
        "evicted": snapshot.evicted,
        "pending_after_drain": snapshot.queued,
        "elapsed_ms": elapsed / 1_000_000,
        "admission_and_dispatch_ops_per_second": (tasks + snapshot.dispatched)
        * 1e9
        / elapsed,
        "scope": "synthetic_policy_microbenchmark_not_market_data_throughput",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=10000)
    parser.add_argument("--queue-capacity", type=int, default=2048)
    parser.add_argument("--cycle-budget", type=int, default=64)
    args = parser.parse_args()
    print(json.dumps(run(args.tasks, args.queue_capacity, args.cycle_budget), indent=2))


if __name__ == "__main__":
    main()
