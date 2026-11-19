"""Telemetry-derived symbol workload cost estimates.

Estimates are admission hints only. Until enough observations exist, unknown
workloads receive deliberately conservative costs and cannot be mistaken for
measured/calibrated capacity.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from aitos.compute.resource_governor import SymbolWorkload


@dataclass(frozen=True)
class SymbolTelemetry:
    symbol: str
    p95_event_rate_per_second: float
    p95_book_depth_levels: int
    analysis_runs_per_minute: float
    sample_count: int

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol cannot be empty")
        if (
            not isfinite(self.p95_event_rate_per_second)
            or not isfinite(self.analysis_runs_per_minute)
            or self.p95_event_rate_per_second < 0
            or self.analysis_runs_per_minute < 0
            or self.p95_book_depth_levels < 0
            or self.sample_count < 0
        ):
            raise ValueError("telemetry values must be finite and non-negative")


@dataclass(frozen=True)
class WorkloadCostModel:
    base_cpu_units: float = 5.0
    cpu_units_per_event_second: float = 0.08
    cpu_units_per_depth_level: float = 0.02
    cpu_units_per_analysis_minute: float = 2.0
    base_memory_mb: int = 16
    bytes_per_book_level: int = 256
    minimum_samples: int = 100
    unknown_cpu_cost_units: float = 100.0
    unknown_memory_cost_mb: int = 128

    def __post_init__(self) -> None:
        numeric = (
            self.base_cpu_units,
            self.cpu_units_per_event_second,
            self.cpu_units_per_depth_level,
            self.cpu_units_per_analysis_minute,
            self.unknown_cpu_cost_units,
        )
        if any(not isfinite(value) or value < 0 for value in numeric):
            raise ValueError("cost model CPU parameters must be finite and non-negative")
        if (
            min(
                self.base_memory_mb,
                self.bytes_per_book_level,
                self.minimum_samples,
                self.unknown_memory_cost_mb,
            )
            < 0
            or self.minimum_samples < 1
        ):
            raise ValueError("cost model memory/sample parameters are invalid")


@dataclass(frozen=True)
class WorkloadEstimate:
    workload: SymbolWorkload
    calibrated: bool
    sample_count: int
    reason: str


def estimate_symbol_workload(
    telemetry: SymbolTelemetry | None,
    *,
    opportunity_score: float = 0.0,
    liquidity_score: float = 0.0,
    mandatory: bool = False,
    open_position: bool = False,
    pending_order: bool = False,
    model: WorkloadCostModel | None = None,
) -> WorkloadEstimate:
    """Convert p95 workload telemetry into a conservative planner input."""
    cost_model = model or WorkloadCostModel()
    if telemetry is None or telemetry.sample_count < cost_model.minimum_samples:
        symbol = telemetry.symbol if telemetry is not None else "UNKNOWN"
        return WorkloadEstimate(
            workload=SymbolWorkload(
                symbol=symbol,
                cpu_cost_units=cost_model.unknown_cpu_cost_units,
                memory_cost_mb=cost_model.unknown_memory_cost_mb,
                opportunity_score=opportunity_score,
                liquidity_score=liquidity_score,
                mandatory=mandatory,
                open_position=open_position,
                pending_order=pending_order,
            ),
            calibrated=False,
            sample_count=0 if telemetry is None else telemetry.sample_count,
            reason="insufficient_telemetry_samples",
        )

    cpu_cost = (
        cost_model.base_cpu_units
        + telemetry.p95_event_rate_per_second
        * cost_model.cpu_units_per_event_second
        + telemetry.p95_book_depth_levels * cost_model.cpu_units_per_depth_level
        + telemetry.analysis_runs_per_minute
        * cost_model.cpu_units_per_analysis_minute
    )
    book_memory_mb = (
        telemetry.p95_book_depth_levels * cost_model.bytes_per_book_level
    ) / (1024 * 1024)
    memory_cost = cost_model.base_memory_mb + int(book_memory_mb + 0.999999)
    return WorkloadEstimate(
        workload=SymbolWorkload(
            symbol=telemetry.symbol,
            cpu_cost_units=cpu_cost,
            memory_cost_mb=memory_cost,
            opportunity_score=opportunity_score,
            liquidity_score=liquidity_score,
            mandatory=mandatory,
            open_position=open_position,
            pending_order=pending_order,
        ),
        calibrated=True,
        sample_count=telemetry.sample_count,
        reason="p95_telemetry_cost_estimate",
    )
