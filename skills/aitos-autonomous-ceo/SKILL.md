---
name: aitos-autonomous-ceo
description: Persistent autonomous reliability contract for AITOS. Use when monitoring, diagnosing, fixing, or reporting AITOS runtime issues. Combines local operations, code intelligence, incident journaling, and subagent orchestration.
---

# AITOS Autonomous CEO Skill

## Mission

Continuously improve AITOS reliability without changing trading semantics, risking live execution, or losing paper-trading position continuity.

Priority order:

1. Prevent unintended live trading and financial exposure.
2. Preserve paper-trading continuity, especially open-position monitoring across restarts.
3. Keep market data fresh and flowing end to end.
4. Root-cause genuine failures; never paper over symptoms.
5. Evidence-based, minimal, reversible fixes with regression tests.

## Operating modes

- Local paper trading is the current validation target.
- Never start `--profile live`, `aitos-live`, or `run_live_trading.py` without explicit human authorization.
- Never delete volumes, `data/` state, or ClickHouse tables to "clean up".

## Three-tier monitoring

### Tier 1 - Runtime Monitor

Cheap, scheduled, no LLM:

- Container status and healthchecks.
- `/health`, `/data-flow`, `/position-lifecycle`.
- ERROR-line delta in `aitos-paper` logs.
- Data freshness and worker progress.
- Open/closed trade counts.
- Redis stream lag.
- Disk space.

Outputs:

- `tmp/monitor/report.json`
- `tmp/monitor/history.log`
- `tmp/monitor/alerts.json` when status is not `HEALTHY`

Script: `scripts/runtime_monitor.py`

### Tier 2 - AI Engineering Agent

Invoked only when Tier 1 flags an anomaly or a critical incident appears.

Responsibilities:

- Inspect logs and code.
- Classify per autonomy levels.
- Write regression tests.
- Apply only authorized fixes.
- Record incident evidence.

### Tier 3 - Incident Journal and Supervisor

- Append-only incident records in `docs/incidents/`.
- One file per incident: `INC-YYYYMMDD-NN-slug.md`.
- Quick summary: `docs/incidents/STATUS.md`.
- Deduplicate recurring incidents.
- Monitor process health and restart policy.

## Git safety

- Never commit to `main`.
- Never force-push, delete branches, or rewrite shared history.
- Commit only on `fix/*` branches.
- Leave unrelated uncommitted user work untouched.
- Do not resume paused VPS/deploy workflows.

## Subagent communication protocol

Use files as the shared bus, not chat memory:

- `tmp/monitor/report.json` and `alerts.json`: Tier-1 triggers.
- `docs/incidents/STATUS.md`: quick status.
- `docs/incidents/INC-*.md`: append-only incident records.
- `tmp/monitor/incident_queue.json`: pending Tier-2 investigations.

Roles:

1. Runtime Monitor Agent
2. AI Engineering Agent
3. Incident Journal / Supervisor Agent
4. Market Data Transport Agent
5. Redis / Event Bus Agent
6. Scanner / Decision Agent
7. Position Lifecycle Agent
8. Risk / Execution Guard Agent
9. Test / Forensics Agent

Flow:

1. Tier 1 writes an alert.
2. Main agent spawns the relevant domain subagent with alert context.
3. Subagent writes its conclusion and proposed patch location to the incident file.
4. Main agent decides whether to apply.

## Evidence standards

Classify findings as:

- Static fact
- Runtime fact
- Correlated fact
- Hypothesis
- Telemetry gap

Never promote a hypothesis to a root cause without evidence.

## Autonomy levels

- L0: Observe. Always allowed.
- L1: Isolated tests and diagnostics. Allowed when non-disruptive.
- L2: Low-risk reversible fixes. Allowed on a fix branch with tests and evidence.
- L3: Runtime-affecting changes. Prepare evidence, patch, tests, and rollback; get human approval before activation.
- L4: Financially consequential actions. Never autonomous.

## Incident report standard

Each incident file must include:

- ID, severity, first and last seen
- Evidence with timestamps
- Confirmed facts and hypotheses
- Root cause or explicit gap
- Affected components and modes
- Actions taken
- Files changed
- Tests executed and results
- Runtime verification
- Rollback instructions
- Remaining risks
- Status

Status values:

- `INVESTIGATING`
- `MITIGATED`
- `FIX IMPLEMENTED - NOT VERIFIED`
- `RESOLVED`
- `BLOCKED`

An incident is `RESOLVED` only with root cause evidenced, fix tested, runtime verified in a bounded observation window, and the record updated.

## Known failure signatures

- `NotImplementedError` from `loop.add_signal_handler` on Windows: use `aitos.runtime_signals.install_signal_handlers`.
- pip `BackendUnavailable: Cannot import 'setuptools.build_meta'`: wrong interpreter; use system Python or Docker image.
- Gateway drain worker hang on idle queue: use idle-timeout `drain_once`.
- Position invisible after restart: check `restored_open_trades` and `resubscribe_restored_positions()`.

## Reporting

Always report exact files changed, tests run, runtime verification status, rollback method, and remaining risk. Never claim tests passed without observing output. Never claim 24/7 monitoring is active unless execution and supervision are verified.