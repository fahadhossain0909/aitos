# Centralized configuration and runtime tuning

AITOS uses a validated tuning snapshot exposed through
`get_settings().tuning` and `load_tuning_settings()`. The same profile
controls scanner workload, analysis concurrency, Event Bus budgets, and
position-management limits. Profiles are capacity presets, not trading advice.

## Resolution order

1. Profile from `aitos/config/tuning_profiles.json`.
2. Mode override (`paper` or `live`).
3. Explicit allowlisted `AITOS_TUNING_*` environment overrides.

Set `AITOS_TUNING_PROFILE` to `low_resource`, `balanced` (default), or
`high_performance`. Set `AITOS_MODE` to `paper` (default) or `live`.
The paper entrypoint requires `AITOS_MODE=paper`; the live entrypoint requires `AITOS_MODE=live` and fails closed on a mismatch.

## Common controls

| Setting | Purpose |
| --- | --- |
| `AITOS_TUNING_SCANNER_TOP_N` | Number of ranked opportunities returned |
| `AITOS_TUNING_SCANNER_UNIVERSE_MODE` | `all_eligible` or `allowlist` |
| `AITOS_TUNING_SCANNER_UNIVERSE_LIMIT` | Maximum configured symbols in the scanner |
| `AITOS_TUNING_SCANNER_SYMBOL_ALLOWLIST` | Comma-separated symbols, e.g. `BTCUSDT,ETHUSDT` |
| `AITOS_TUNING_SCANNER_MIN_LIQUIDITY_QUALITY` | Hard 0–10 execution-quality floor (default `5.0`) |
| `AITOS_TUNING_SCANNER_MAX_DIRECTIONAL_CANCEL_PRESSURE` | Fresh, side-specific depth cancellation veto threshold (default `0.6`) |
| `AITOS_TUNING_ANALYSIS_EXPENSIVE_ANALYSIS_LIMIT` | Maximum symbols receiving full analysis per scan cycle |
| `AITOS_TUNING_ANALYSIS_EXPENSIVE_ANALYSIS_CONCURRENCY` | Concurrent full-analysis tasks |
| `AITOS_TUNING_ANALYSIS_LIGHTWEIGHT_CONCURRENCY` | Concurrent short-candle screening tasks |
| `AITOS_TUNING_ANALYSIS_TIMEOUT_SECONDS` | Per-symbol analysis timeout |
| `AITOS_TUNING_EVENT_BUS_CONSUMER_CONCURRENCY` | Concurrent independent stream handlers |
| `AITOS_TUNING_EVENT_BUS_CONNECTION_POOL_SIZE` | Redis connection pool size |
| `AITOS_TUNING_EVENT_BUS_CONSUMER_BATCH_SIZE` | Redis Stream read batch size |
| `AITOS_TUNING_POSITIONS_MAX_OPEN_POSITIONS` | Profile hard cap for open positions |
| `AITOS_TUNING_POSITIONS_MAX_POSITIONS_PER_SYMBOL` | Per-symbol position cap |
| `AITOS_TUNING_POSITIONS_MONITORING_INTERVAL_SECONDS` | Minimum regular full-EIE cadence per open position |
| `AITOS_TUNING_POSITIONS_EMERGENCY_STOP_R_MULTIPLE` | Catastrophic stop distance in initial soft-stop R multiples (default `2.0`; valid range `>1` to `10`) |
| `AITOS_TUNING_POSITIONS_INTENSIVE_ANALYSIS_BUDGET` | Legacy compatibility field; no longer a shared global EIE gate |
| `AITOS_TUNING_POSITIONS_DEAD_CANDLE_EXIT_BARS` | Unique completed 15m candles before a verified flatline scratch exit (default `16`) |
| `AITOS_TUNING_POSITIONS_DEAD_CANDLE_MAX_PROGRESS_R` | Maximum recorded MFE allowed for the dead-candle exit (default `0.10R`) |
| `AITOS_TUNING_POSITIONS_DEAD_CANDLE_ENTRY_BAND_PCT` | Maximum entry-relative excursion for dead-flat classification (default `0.002`, or `0.2%`) |
| `AITOS_TUNING_POSITIONS_STALE_POSITION_MAX_HOURS` | Paper-runtime flatline fallback age (default `6h`; not a blanket age exit) |
| `AITOS_TUNING_RUNTIME_CONFIG_RELOAD_ENABLED` | Opt in to safe reload attempts between scan cycles |

Allowlist mode requires a non-empty allowlist. Boolean overrides accept
`true/false`, `yes/no`, `on/off`, or `1/0`; invalid values fail loudly.

## Examples

Bash:

```bash
AITOS_TUNING_PROFILE=low_resource AITOS_TUNING_SCANNER_TOP_N=2 python run_paper_trading.py
```

PowerShell:

```powershell
$env:AITOS_TUNING_PROFILE = "low_resource"
$env:AITOS_TUNING_SCANNER_TOP_N = "2"
python run_paper_trading.py
```

To restrict the scanner to two markets:

```bash
AITOS_TUNING_SCANNER_UNIVERSE_MODE=allowlist AITOS_TUNING_SCANNER_SYMBOL_ALLOWLIST=BTCUSDT,ETHUSDT python run_paper_trading.py
```

## Runtime behavior and safety

- The scanner first scores a short candle window, then runs expensive
  order-flow/liquidity/funding/open-interest analysis only for the top
  `expensive_analysis_limit` symbols. The full scanner remains authoritative
  for direction and trade eligibility.
- Full Exit Intelligence runs on an independent cadence for each open position;
  one active symbol cannot consume another position's evaluation allowance.
- A soft-stop breach or TP boundary forces an immediate EIE evaluation. With
  EIE enabled, the emergency stop is separate from the initial soft stop and
  position sizing uses the emergency distance so the configured risk amount is
  preserved. Without EIE, the original SL behavior remains unchanged.
- If EIE fails while a soft stop is breached, the lifecycle fails closed at
  that boundary. The emergency stop is never overridden by HOLD.
- Startup logs contain a stable `config_version` hash and the effective
  non-secret tuning values for auditability.
- `reload_tuning_settings()` refreshes the profile file and validates a new
  snapshot. `SystemComponents.reload_runtime_tuning()` applies runtime-safe
  scanner/position budgets. Changes to the market-data universe, Redis pool or
  consumer budgets, and the hard max-open-position cap require restart; rejected
  automatic reloads keep the last known-good snapshot active and log the reason.
- Runtime reload and adaptive tuning are disabled by default. Adaptive tuning
  does not autonomously change risk limits.
- A tuning profile never raises a caller-provided max-open-position risk limit.
  Profile numbers are example capacity presets, not measured optimal values.
- Live execution remains governed by the existing execution adapter, risk
  engine and approval controls. Do not use a capacity profile as a substitute
  for risk validation.
- Secrets are not stored in the profile file.


## Configuration domains and implementation status

The current repository keeps one authoritative Pydantic schema in
`aitos/config/tuning.py` and capacity presets in
`aitos/config/tuning_profiles.json`. This is intentionally a smaller migration
than introducing a new YAML tree and duplicate defaults. The schema now groups
values into scanner, analysis, Event Bus, positions, hardware, storage,
telemetry, and runtime sections. Profile values are validated and included in
the effective configuration hash.

### Risk ceiling overrides

The risk section is deliberately conservative. Central tuning can lower, but
not raise, the current baseline ceilings. It is applied by `build_system()`
using the stricter of the supplied risk-engine value and the central ceiling.

- `AITOS_TUNING_RISK_MAX_RISK_PER_TRADE_PCT` (maximum 1.0)
- `AITOS_TUNING_RISK_MAX_LEVERAGE` (maximum 10.0)
- `AITOS_TUNING_RISK_MAX_DRAWDOWN_PCT` (maximum 10.0)
- `AITOS_TUNING_RISK_MAX_CORRELATED_EXPOSURE_PCT` (maximum 15.0)
- `AITOS_TUNING_RISK_MAX_SECTOR_EXPOSURE_PCT` (maximum 20.0)

### Additional environment overrides

- `AITOS_TUNING_HARDWARE_CPU_WORKER_BUDGET`
- `AITOS_TUNING_HARDWARE_MEMORY_BUDGET_MB`
- `AITOS_TUNING_HARDWARE_MEMORY_RESERVE_PCT`
- `AITOS_TUNING_HARDWARE_MAX_BACKGROUND_TASKS`
- `AITOS_TUNING_STORAGE_HISTORICAL_BACKFILL_CONCURRENCY`
- `AITOS_TUNING_STORAGE_CLICKHOUSE_BATCH_SIZE`
- `AITOS_TUNING_STORAGE_CLICKHOUSE_QUERY_TIMEOUT_SECONDS`
- `AITOS_TUNING_STORAGE_RETENTION_DAYS`
- `AITOS_TUNING_TELEMETRY_HEALTH_INTERVAL_SECONDS`
- `AITOS_TUNING_TELEMETRY_METRICS_SAMPLE_INTERVAL_SECONDS`
- `AITOS_TUNING_TELEMETRY_TRACE_SAMPLE_RATE`

### Important scope boundary

The scanner's expensive-analysis semaphore is capped by both its configured
concurrency and the hardware profile's `cpu_worker_budget`. Central risk ceilings
are wired into `build_system()` and can tighten, but never relax, caller-supplied
risk limits. Hardware memory
budget/reserve, background-task limits, storage backfill/batch/retention values,
and telemetry sampling intervals are currently validated configuration
contracts and profile hints; they are **not yet universal runtime governors**.
They must not be interpreted as proof that the running process is enforcing
those limits in every storage, health, metrics, or background worker. Automatic
CPU/RAM detection, adaptive resource control, a dashboard editor, and complete
inventory/removal of all legacy strategy/execution constants are also not
implemented by this PR.

The schema rejects adaptive tuning in live mode. It does not let hardware
profiles increase the financial risk limits of a caller-supplied risk engine.
Before declaring this initiative complete, these staged settings must be wired
to their owning components and covered by runtime integration tests; use the
effective config report to distinguish configured intent from measured runtime
behavior.
