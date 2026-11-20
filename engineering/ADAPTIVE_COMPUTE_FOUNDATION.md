# Adaptive Compute Foundation

This phase adds a hardware-aware, risk-neutral capacity planning primitive.
It is intentionally additive: existing tuning profiles, scanner decisions,
market-data authority, execution, and risk limits remain authoritative.

## Runtime resource discovery

`aitos.compute.resource_governor.detect_resources()` reports resources visible
to the process. On Linux it considers CPU affinity, cgroup v2 `cpu.max` and
`memory.max`, and the cgroup v1 CPU/memory equivalents. It also reports
available memory conservatively and detects visible NVIDIA/DRM device nodes.
A missing procfs/cgroup/device path is tolerated, so non-Linux environments
fall back to process-visible CPU count and zero/unknown memory values.

The detector does not run shell commands, load GPU runtimes, initialize CUDA,
or assume that a visible device is usable. GPU execution requires a later
backend-specific compatibility and benchmark gate.

## Capacity and admission

`build_resource_budget()` applies an explicit memory reserve and CPU
utilization target. `plan_symbol_capacity()` ranks optional symbol workloads
by opportunity/liquidity value per estimated compute cost while preserving
all symbols with open positions, pending orders, or an explicit mandatory
flag. Protected workload overcommit is surfaced instead of silently dropping
protection. The caller must respond by suppressing optional work and preserving
position/order monitoring.

The planner is a pure function. It does not fetch exchange data, mutate the
scanner, open positions, change risk limits, or place orders. Cost units and
memory estimates must be calibrated from measured production-like telemetry;
the defaults are placeholders, not measured per-symbol costs.

## Pressure signals

`classify_pressure()` converts CPU, memory, and queue-lag observations into
NORMAL/WARNING/HIGH/CRITICAL advisory levels. It does not autonomously pause
workers or modify strategy parameters. Runtime integration must add hysteresis,
cooldowns, observability, and explicit service ownership before any automatic
degradation is enabled.

## Safety boundaries

- Hardware capacity never changes leverage, position sizing, strategy thresholds,
  capital limits, or execution authority.
- Open-position and pending-order symbols are protected from capacity eviction.
- Invalid or stale market data must continue to fail closed under existing
  market-data and execution contracts.
- CPU is the universal fallback. Accelerator presence is only a capability hint;
  it is not proof of a compatible runtime or faster end-to-end processing.
- This phase is not a claim of measured throughput, live readiness, or GPU support.


## Adaptive compute runtime control (PR #261)

The compute foundation now includes an advisory pressure controller with
separate escalation and recovery dwell times. CRITICAL pressure is surfaced
immediately; WARNING/HIGH escalation and all recovery transitions use
hysteresis. A monotonic clock is required so clock corrections cannot cause
early recovery.

Validated `RuntimeTuning` exposes the policy values and an explicit
`adaptive_compute_enabled` switch. The switch defaults to false and is
rejected in live mode. Environment overrides use the
`AITOS_TUNING_RUNTIME_ADAPTIVE_COMPUTE_ENABLED` and
`AITOS_TUNING_RUNTIME_PRESSURE_*_SECONDS` names.

The controller is exposed through the paper-only adaptive runtime. The runtime
factory is called during application construction, but returns no controller
unless the explicit opt-in is enabled; it fails closed if a caller attempts to
enable it in live mode. The runtime exposes one pressure update and optional
admission interface, so service owners can apply the same pressure state to
bounded CPU/memory budgets.

The scanner integration is deliberately narrow: the lightweight candidate
screen may reduce the optional expensive-analysis cohort using the resource
budget and observed per-symbol workload. It protects the configured reference
symbol and receives current open-position and pending-order symbols from the
portfolio/lifecycle snapshot before each scan cycle. If the adaptive planner
fails, the scanner falls back to its existing fixed candidate cap. Market-data
ingestion, order updates, position monitoring, risk checks, execution recovery,
and trading strategy decisions are not scheduled by this optional-work planner
and are never deferred by it.

## Telemetry-based workload estimation

A separate estimator now converts per-symbol p95 event rate, order-book depth,
and analysis frequency into CPU/memory cost hints. Estimates are marked
uncalibrated until the configured minimum sample count is met; unknown symbols
receive conservative fallback costs instead of optimistic zero-cost admission.
The cost coefficients remain explicit calibration parameters and must be
benchmarked against production-like telemetry before scanner admission uses
them.


## Optional workload admission

The optional-work scheduler ranks interactive analysis ahead of background and
bulk work, applies CPU/memory hard budgets, and progressively shrinks optional
capacity under WARNING/HIGH pressure. CRITICAL pressure defers all optional
work. It explicitly does not own protected services; market-data integrity,
order updates, open-position monitoring, risk checks, and execution recovery
must be scheduled by their own protected service budgets.


## Scanner telemetry and opt-in operation

When adaptive compute is enabled in paper mode, the application creates a
bounded 60-second, process-local telemetry collector. The scanner records
accepted trade events, observed book depth, and analysis starts. Per-symbol
sample storage is capped and stale samples expire; symbols are LRU-bounded to
avoid unbounded cardinality. An estimate is considered calibrated only when
the event, depth, and analysis streams all meet the estimator's minimum sample
count. Until then, the estimator uses its conservative unknown-workload cost.

Enable only in paper environments after validating resource limits and
telemetry coverage:

```bash
AITOS_TUNING_RUNTIME_ADAPTIVE_COMPUTE_ENABLED=true
```

The default remains disabled. No live trading configuration should enable this
feature. The scanner's adaptive capacity is a compute admission decision only;
it does not change signal thresholds, candidate scores, risk ceilings, order
quantity, leverage, or execution authority.
