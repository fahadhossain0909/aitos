from aitos.compute.resource_governor import (
    PressureLevel,
    ResourceSnapshot,
    SymbolWorkload,
    build_resource_budget,
    classify_pressure,
    detect_resources,
    plan_symbol_capacity,
)


def test_detect_resources_honors_cgroup_v2_limits(tmp_path):
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    devices = tmp_path / "dev"
    proc.mkdir()
    cgroup.mkdir()
    devices.mkdir()
    (proc / "meminfo").write_text(
        "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n",
        encoding="ascii",
    )
    (cgroup / "cpu.max").write_text("200000 100000", encoding="ascii")
    (cgroup / "memory.max").write_text(str(6 * 1024 * 1024 * 1024), encoding="ascii")
    (cgroup / "memory.current").write_text(
        str(2 * 1024 * 1024 * 1024), encoding="ascii"
    )

    result = detect_resources(proc_root=proc, cgroup_root=cgroup, device_root=devices)

    assert result.effective_cpus <= 2.0
    assert result.memory_limit_mb == 6144
    assert result.memory_available_mb == 4096
    assert result.memory_limit_limited is True


def test_budget_reserves_memory_and_limits_workers():
    resources = ResourceSnapshot(8, 8.0, 16384, 8192, 7000, False, True)
    budget = build_resource_budget(resources, memory_reserve_pct=25)

    assert budget.cpu_workers == 8
    assert budget.memory_budget_mb == 6144
    assert budget.background_task_limit == 4
    assert budget.cpu_capacity_units == 600.0


def test_budget_uses_available_memory_when_lower_than_reserve_budget():
    resources = ResourceSnapshot(4, 4.0, 8192, 8192, 1200, False, False)
    budget = build_resource_budget(resources)

    assert budget.memory_budget_mb == 1200


def test_symbol_planner_prioritizes_value_per_compute_cost():
    budget = build_resource_budget(
        ResourceSnapshot(2, 2.0, 6144, 6144, 5000, False, False)
    )
    plan = plan_symbol_capacity(
        [
            SymbolWorkload(
                "LOW", cpu_cost_units=80, memory_cost_mb=100, opportunity_score=1
            ),
            SymbolWorkload(
                "HIGH", cpu_cost_units=40, memory_cost_mb=100, opportunity_score=9
            ),
            SymbolWorkload(
                "MID", cpu_cost_units=40, memory_cost_mb=100, opportunity_score=5
            ),
        ],
        budget,
    )

    assert plan.selected_symbols == ("HIGH", "MID")
    assert plan.deferred_symbols == ("LOW",)


def test_open_positions_and_pending_orders_are_never_dropped():
    budget = build_resource_budget(
        ResourceSnapshot(1, 1.0, 1024, 1024, 500, False, False)
    )
    plan = plan_symbol_capacity(
        [
            SymbolWorkload(
                "BTCUSDT", cpu_cost_units=1000, memory_cost_mb=900, open_position=True
            ),
            SymbolWorkload(
                "ETHUSDT", cpu_cost_units=1000, memory_cost_mb=900, pending_order=True
            ),
            SymbolWorkload(
                "NEWUSDT", cpu_cost_units=1, memory_cost_mb=1, opportunity_score=10
            ),
        ],
        budget,
        max_symbols=1,
    )

    assert set(plan.protected_symbols) == {"BTCUSDT", "ETHUSDT"}
    assert set(plan.selected_symbols) == {"BTCUSDT", "ETHUSDT"}
    assert plan.overcommitted_by_protected_work is True
    assert "NEWUSDT" in plan.deferred_symbols


def test_duplicate_symbols_are_deduplicated_without_losing_protection():
    budget = build_resource_budget(
        ResourceSnapshot(2, 2.0, 4096, 4096, 3000, False, False)
    )
    plan = plan_symbol_capacity(
        [
            SymbolWorkload("btcusdt", opportunity_score=10),
            SymbolWorkload("BTCUSDT", open_position=True),
        ],
        budget,
    )

    assert len(plan.selected_symbols) == 1
    assert plan.protected_symbols == ("BTCUSDT",)


def test_pressure_classification_has_ordered_levels():
    assert (
        classify_pressure(
            cpu_utilization_pct=20, memory_utilization_pct=30, queue_lag_seconds=0.1
        )
        == PressureLevel.NORMAL
    )
    assert (
        classify_pressure(
            cpu_utilization_pct=82, memory_utilization_pct=30, queue_lag_seconds=0.1
        )
        == PressureLevel.WARNING
    )
    assert (
        classify_pressure(
            cpu_utilization_pct=92, memory_utilization_pct=30, queue_lag_seconds=0.1
        )
        == PressureLevel.HIGH
    )
    assert (
        classify_pressure(
            cpu_utilization_pct=30, memory_utilization_pct=30, queue_lag_seconds=12
        )
        == PressureLevel.CRITICAL
    )


def test_invalid_budget_and_workload_inputs_are_rejected():
    resources = ResourceSnapshot(2, 2.0, 4096, 4096, 3000, False, False)
    try:
        build_resource_budget(resources, memory_reserve_pct=0)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid reserve must be rejected")
    try:
        SymbolWorkload("BAD", cpu_cost_units=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative workload cost must be rejected")


def test_zero_available_memory_produces_zero_optional_budget():
    resources = ResourceSnapshot(2, 2.0, 4096, 4096, 0, False, False)
    budget = build_resource_budget(resources)
    assert budget.memory_budget_mb == 0


def test_high_scoring_duplicate_cannot_replace_protected_symbol():
    budget = build_resource_budget(
        ResourceSnapshot(2, 2.0, 4096, 4096, 3000, False, False)
    )
    plan = plan_symbol_capacity(
        [
            SymbolWorkload("BTCUSDT", open_position=True, opportunity_score=0),
            SymbolWorkload("btcusdt", opportunity_score=1000),
        ],
        budget,
    )
    assert plan.protected_symbols == ("BTCUSDT",)
    assert plan.selected_symbols == ("BTCUSDT",)


def test_non_finite_workload_and_pressure_values_are_rejected():
    try:
        SymbolWorkload("NAN", cpu_cost_units=float("nan"))
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite workload cost must be rejected")

    try:
        classify_pressure(
            cpu_utilization_pct=float("nan"),
            memory_utilization_pct=0,
            queue_lag_seconds=0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite pressure metrics must be rejected")



def test_pressure_controller_escalates_and_recovers_with_hysteresis():
    from aitos.compute.pressure_controller import (
        PressurePolicy,
        ResourcePressureController,
    )

    controller = ResourcePressureController(
        PressurePolicy(
            warning_hold_seconds=2,
            high_hold_seconds=2,
            recovery_hold_seconds=5,
            minimum_transition_interval_seconds=0,
        )
    )
    first = controller.update(
        now_seconds=1,
        cpu_utilization_pct=95,
        memory_utilization_pct=20,
        queue_lag_seconds=0,
    )
    assert first.level == PressureLevel.NORMAL
    second = controller.update(
        now_seconds=3,
        cpu_utilization_pct=95,
        memory_utilization_pct=20,
        queue_lag_seconds=0,
    )
    assert second.level == PressureLevel.HIGH
    assert second.defer_optional_work is True
    recovering = controller.update(
        now_seconds=4,
        cpu_utilization_pct=10,
        memory_utilization_pct=20,
        queue_lag_seconds=0,
    )
    assert recovering.level == PressureLevel.HIGH
    recovered = controller.update(
        now_seconds=9,
        cpu_utilization_pct=10,
        memory_utilization_pct=20,
        queue_lag_seconds=0,
    )
    assert recovered.level == PressureLevel.NORMAL
    assert recovered.allow_background_work is True


def test_pressure_controller_critical_is_immediate_and_clock_is_monotonic():
    from aitos.compute.pressure_controller import ResourcePressureController

    controller = ResourcePressureController()
    critical = controller.update(
        now_seconds=10,
        cpu_utilization_pct=99,
        memory_utilization_pct=20,
        queue_lag_seconds=0,
    )
    assert critical.level == PressureLevel.CRITICAL
    assert critical.defer_optional_work is True
    try:
        controller.update(
            now_seconds=9,
            cpu_utilization_pct=0,
            memory_utilization_pct=0,
            queue_lag_seconds=0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-monotonic clock must be rejected")
