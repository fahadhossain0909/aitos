# Centralized configuration and runtime tuning

AITOS has a validated tuning-profile layer exposed through
`get_settings().tuning` and `load_tuning_settings()`. This is a staged
migration: the typed contract and profile resolver are centralized first;
existing modules must be migrated deliberately rather than silently changing
trading behavior all at once.

## Resolution order

1. Hardware/workload profile from `aitos/config/tuning_profiles.json`.
2. Mode-specific override (`paper` or `live`).
3. Explicit allowlisted `AITOS_TUNING_*` environment overrides.

Set `AITOS_TUNING_PROFILE` to `low_resource`, `balanced` (default), or
`high_performance`. Set `AITOS_MODE` to `paper` (default) or `live`.

## Example

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

Initial parameters include scanner top-N and universe selection, expensive
analysis budget/concurrency, Event Bus consumer/queue budgets, open-position
limits, per-symbol limits, and monitoring cadence. Use
`AITOS_TUNING_SCANNER_SYMBOL_ALLOWLIST=BTCUSDT,ETHUSDT` with
`AITOS_TUNING_SCANNER_UNIVERSE_MODE=allowlist`; allowlist mode without symbols
is rejected.

## Safety and limitations

- Profile numbers are example capacity presets, not measured optimal values.
- Hardware profiles do not raise per-trade risk, leverage, or total exposure.
- Live execution remains governed by the existing execution adapter, risk
  engine and approval controls.
- Adaptive tuning and runtime config reload remain disabled by default.
- Invalid profile names, malformed values, and out-of-range settings fail
  validation instead of silently falling back.
- Secrets are not stored in the tuning profile file.
- This PR introduces the centralized contract, resolver and tests. Wiring every
  existing scanner, Event Bus, execution and position-management constant into
  the contract remains follow-up migration work; it is intentionally not
  represented as complete here.
