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


def test_adaptive_compute_tuning_is_opt_in_and_live_rejected():
    from pydantic import ValidationError

    from aitos.config.tuning import TuningSettings

    defaults = TuningSettings()
    assert defaults.runtime.adaptive_compute_enabled is False
    assert defaults.runtime.pressure_recovery_hold_seconds == 30.0

    try:
        TuningSettings(
            mode="live",
            runtime={"adaptive_compute_enabled": True},
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("adaptive compute must remain disabled in live mode")


def test_adaptive_compute_pressure_policy_is_validated():
    from pydantic import ValidationError

    from aitos.config.tuning import RuntimeTuning

    policy = RuntimeTuning(
        pressure_warning_hold_seconds=7,
        pressure_recovery_hold_seconds=60,
    )
    assert policy.pressure_warning_hold_seconds == 7
    assert policy.pressure_recovery_hold_seconds == 60
    try:
        RuntimeTuning(pressure_recovery_hold_seconds=-1)
    except ValidationError:
        pass
    else:
        raise AssertionError("negative recovery hold must be rejected")


def test_adaptive_compute_runtime_factory_is_disabled_by_default():
    from aitos.compute.adaptive_runtime import create_adaptive_compute_runtime
    from aitos.config.tuning import TuningSettings

    assert create_adaptive_compute_runtime(TuningSettings()) is None


def test_adaptive_compute_runtime_respects_detected_and_configured_caps():
    from aitos.compute.adaptive_runtime import create_adaptive_compute_runtime
    from aitos.config.tuning import TuningSettings

    settings = TuningSettings(
        runtime={"adaptive_compute_enabled": True},
        hardware={
            "cpu_worker_budget": 2,
            "memory_budget_mb": 1024,
            "memory_reserve_pct": 25,
            "max_background_tasks": 1,
        },
    )
    runtime = create_adaptive_compute_runtime(
        settings,
        resources=ResourceSnapshot(8, 8.0, 16384, 8192, 4096, False, True),
    )
    assert runtime is not None
    assert runtime.budget.cpu_workers == 2
    assert runtime.resources.memory_limit_mb == 1024
    assert runtime.budget.memory_budget_mb == 768
    assert runtime.budget.background_task_limit == 1


def test_adaptive_compute_runtime_factory_fails_closed_for_live_mode():
    from aitos.compute.adaptive_runtime import create_adaptive_compute_runtime
    from aitos.config.tuning import TuningSettings

    settings = TuningSettings.model_construct(
        mode="live",
        runtime=TuningSettings().runtime.model_copy(
            update={"adaptive_compute_enabled": True}
        ),
        hardware=TuningSettings().hardware,
    )
    try:
        create_adaptive_compute_runtime(settings)
    except ValueError:
        pass
    else:
        raise AssertionError("live adaptive compute must fail closed")


def test_workload_estimator_uses_conservative_cost_until_calibrated():
    from aitos.compute.workload_estimator import (
        WorkloadCostModel,
        estimate_symbol_workload,
    )

    estimate = estimate_symbol_workload(
        None,
        opportunity_score=9,
        model=WorkloadCostModel(
            minimum_samples=50,
            unknown_cpu_cost_units=250,
            unknown_memory_cost_mb=512,
        ),
    )
    assert estimate.calibrated is False
    assert estimate.workload.cpu_cost_units == 250
    assert estimate.workload.memory_cost_mb == 512


def test_workload_estimator_scales_cost_with_event_rate_depth_and_analysis():
    from aitos.compute.workload_estimator import (
        SymbolTelemetry,
        WorkloadCostModel,
        estimate_symbol_workload,
    )

    estimate = estimate_symbol_workload(
        SymbolTelemetry(
            symbol="BTCUSDT",
            p95_event_rate_per_second=100,
            p95_book_depth_levels=1000,
            analysis_runs_per_minute=5,
            sample_count=1000,
        ),
        model=WorkloadCostModel(minimum_samples=100),
    )
    assert estimate.calibrated is True
    assert estimate.workload.cpu_cost_units > 20
    assert estimate.workload.memory_cost_mb >= 16
    assert estimate.reason == "p95_telemetry_cost_estimate"


def test_workload_estimator_rejects_invalid_telemetry():
    from aitos.compute.workload_estimator import SymbolTelemetry

    try:
        SymbolTelemetry("BTCUSDT", float("nan"), 10, 1, 100)
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite telemetry must be rejected")

def test_optional_scheduler_applies_priority_and_hard_budgets():
    from aitos.compute.work_scheduler import (
        WorkloadClass,
        WorkloadRequest,
        admit_optional_work,
    )

    result = admit_optional_work(
        [
            WorkloadRequest("backfill", WorkloadClass.BULK, 10, 10),
            WorkloadRequest("scan-a", WorkloadClass.INTERACTIVE, 30, 20, 2),
            WorkloadRequest("scan-b", WorkloadClass.INTERACTIVE, 30, 20, 1),
            WorkloadRequest("metrics", WorkloadClass.BACKGROUND, 10, 10),
        ],
        cpu_capacity_units=60,
        memory_capacity_mb=40,
    )
    assert result.admitted == ("scan-a", "scan-b")
    assert set(result.deferred) == {"metrics", "backfill"}
    assert result.cpu_reserved_units == 60

def test_optional_scheduler_reduces_work_during_warning_and_high_pressure():
    from aitos.compute.work_scheduler import (
        WorkloadClass,
        WorkloadRequest,
        admit_optional_work,
    )

    requests = [
        WorkloadRequest("interactive", WorkloadClass.INTERACTIVE, 5, 2),
        WorkloadRequest("background", WorkloadClass.BACKGROUND, 20, 10),
    ]
    warning = admit_optional_work(
        requests,
        cpu_capacity_units=40,
        memory_capacity_mb=20,
        pressure_level=1,
    )
    assert warning.admitted == ("interactive",)
    high = admit_optional_work(
        requests,
        cpu_capacity_units=40,
        memory_capacity_mb=20,
        pressure_level=2,
    )
    assert high.admitted == ("interactive",)
    assert high.deferred == ("background",)
    critical = admit_optional_work(
        requests,
        cpu_capacity_units=40,
        memory_capacity_mb=20,
        pressure_level=3,
    )
    assert critical.admitted == ()
    assert set(critical.deferred) == {"interactive", "background"}

def test_optional_scheduler_rejects_duplicate_ids_and_invalid_budgets():
    from aitos.compute.work_scheduler import (
        WorkloadClass,
        WorkloadRequest,
        admit_optional_work,
    )

    request = WorkloadRequest("same", WorkloadClass.INTERACTIVE, 1)
    try:
        admit_optional_work(
            [request, request], cpu_capacity_units=1, memory_capacity_mb=1
        )
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate request ids must be rejected")

    try:
        admit_optional_work([], cpu_capacity_units=float("nan"), memory_capacity_mb=1)
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite capacity must be rejected")



def test_symbol_telemetry_collector_exports_bounded_p95_samples():
    from aitos.compute.telemetry_collector import SymbolTelemetryCollector

    now = [100.0]
    collector = SymbolTelemetryCollector(
        window_seconds=10,
        max_symbols=2,
        max_samples_per_metric=5,
        clock=lambda: now[0],
    )
    for offset in range(5):
        collector.record_event("BTCUSDT", timestamp=95 + offset)
    collector.record_book_depth("BTCUSDT", 20, timestamp=99)
    collector.record_book_depth("BTCUSDT", 40, timestamp=100)
    collector.record_analysis("BTCUSDT", timestamp=99)
    collector.record_analysis("BTCUSDT", timestamp=100)

    snapshot = collector.snapshot("btcusdt")
    assert snapshot is not None
    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.p95_event_rate_per_second >= 1
    assert snapshot.p95_book_depth_levels == 40
    assert snapshot.analysis_runs_per_minute == 12
    assert snapshot.sample_count == 9


def test_symbol_telemetry_collector_prunes_old_samples_and_bounds_cardinality():
    from aitos.compute.telemetry_collector import SymbolTelemetryCollector

    now = [10.0]
    collector = SymbolTelemetryCollector(
        window_seconds=5,
        max_symbols=2,
        max_samples_per_metric=2,
        clock=lambda: now[0],
    )
    collector.record_event("BTCUSDT", timestamp=1)
    collector.record_event("BTCUSDT", timestamp=9)
    collector.record_event("ETHUSDT", timestamp=9)
    collector.record_event("SOLUSDT", timestamp=9)

    assert collector.snapshot("BTCUSDT") is None
    assert collector.snapshot("ETHUSDT") is not None
    assert collector.snapshot("SOLUSDT") is not None
    assert collector.stats().tracked_symbols == 2


def test_symbol_telemetry_collector_rejects_invalid_samples():
    from aitos.compute.telemetry_collector import SymbolTelemetryCollector

    collector = SymbolTelemetryCollector()
    try:
        collector.record_event("BTCUSDT", timestamp=float("nan"))
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite timestamp must be rejected")
