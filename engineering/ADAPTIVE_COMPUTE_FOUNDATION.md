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
