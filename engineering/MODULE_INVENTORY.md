# AITOS Module Inventory

## Status legend
- DOCUMENTED: stated by current README or existing repository skill; not proof of runtime operation.
- SOURCE-VERIFIED: implementation and call paths inspected.
- RUNTIME-VERIFIED: observed working in a named environment with evidence.
- PARTIAL / DISCONNECTED / BROKEN: use only with evidence.
- NOT INSPECTED: not yet audited.

## Initial inventory
| Subsystem | Known repository surface | Current status | Required verification |
|---|---|---|---|
| Paper runner | `run_paper_trading.py` | DOCUMENTED | Startup, dependencies, actual event consumption, paper fills, health |
| Live runner | `run_live_trading.py` | DOCUMENTED; safety-critical | Static review only unless explicitly authorized; never submit live orders |
| Continual learning | `run_continual_learning.py` | DOCUMENTED | Worker lifecycle, input/output, resource limits |
| Exchange adapter | Binance USDT-M REST/WebSocket | DOCUMENTED | Endpoint, subscriptions, event freshness, reconnect/fallback |
| Event transport | Redis Streams / Event Bus | DOCUMENTED | Groups, offsets, pending, ACK, DLQ, lag, throughput |
| Historical persistence | ClickHouse | DOCUMENTED | Inserts, schema, query latency, retention, source-of-truth semantics |
| Knowledge graph | Optional Neo4j | DOCUMENTED | Activation gates, callers, failure isolation |
| Opportunity scanner | Scanner / candidate intelligence | DOCUMENTED | Candidate throughput, rotation, ranking, stale state |
| Risk controls | Risk engine / circuit breaker / exposure | DOCUMENTED | Exact call sites and paper/live separation |
| Trade lifecycle | Position/order lifecycle | DOCUMENTED | Persistence, partial exits, close confirmation, restart recovery |
| Backtesting | `aitos.backtest.cli`, `rich_cli` | DOCUMENTED | Test coverage and parity with live decision contracts |
| Health/metrics | `aitos.health_server`; `/health`, `/metrics` documented | DOCUMENTED | Actual bind address, module readiness semantics, scrape |
| Monitoring utility | `monitor_aitos.py` | SOURCE SURFACE FOUND | Inspect all checks for assumptions, false positives, side effects |
| Agent instructions | `skills/aitos-skill.md`, `skills/aitos-prompt.md` | SOURCE FOUND | Reconcile with AGENTS.md; skill is mandatory operational reference |

## Source-level audit updates (2026-11-03)
- `aitos/health_server.py`: SOURCE-VERIFIED. Exposes `/health`, `/metrics`, `/data-flow`, `/data-flow/{symbol}`, `/position-lifecycle`, and position-specific lifecycle diagnostics. Runtime reachability is NOT TESTED.
- `aitos/forensics/data_flow_registry.py` and `data_flow_schema.py`: SOURCE-VERIFIED. In-process stage registry retains up to 2,048 latency observations per bucket, reports p50/p95/p99/max, input/output rates, queue depth/age, source age, drops/errors, and recent trace records. This bounded in-memory sample is not durable history.
- `monitoring/prometheus.yml`: SOURCE-VERIFIED. Scrape interval is 15 seconds for paper and live targets. Actual Prometheus scrape success is NOT TESTED.
- `deploy/grafana/aitos_data_flow.json`: SOURCE-VERIFIED dashboard queries exist for p50/p95/p99 data-flow metrics. Dashboard provisioning and rendered panel correctness are NOT TESTED.
- AITOS-ENG-004 remediation is proposed in PR #217: missing percentile series are omitted and a per-stage sample-count metric is exported. CI/runtime verification pending.

## Scanner and position lifecycle telemetry source audit (2026-11-03)
- `aitos/intelligence/scanner.py::scan_symbol()`: SOURCE-VERIFIED. Uses monotonic `time.perf_counter()`, records scanner-stage input/output latency, emits trace start/completion, records processing errors, and notes whether a candidate was found.
- `aitos/app.py::run_scan_and_trade_cycle()`: SOURCE-VERIFIED. Logs candidate rank, direction, composite/component scores, weights and threshold; logs per-symbol position-cap skips, kernel rejection reasons/confidence, publishes `decision.snapshot`, and records lifecycle/risk rejection vs accepted/submitted states. Candidate rotation snapshots are included in cycle diagnostics.
- `aitos/intelligence/position_runtime.py`: SOURCE-VERIFIED. Emits per-position evaluation duration, monitor tier/priority, selected health fields (including CVD/delta, liquidity/volatility/regime/reference/thesis risk and data freshness), action and reason; lifecycle wrappers emit close attempt/completion/failure and monitor heartbeat/staleness events.
- `aitos/telemetry/position_lifecycle.py` plus `docs/POSITION_LIFECYCLE_TELEMETRY.md`: structured JSON log events are durable via the configured log sink, while the HealthServer ring buffer is process-local and capped at 2,000. `scripts/position_lifecycle_telemetry_report.py` can summarize JSON-lines logs without querying or mutating trading state.
- This confirms useful decision and lifecycle event coverage in source; actual event emission, log retention, completeness and latency distributions remain NOT RUNTIME-VERIFIED.

## Paper-runtime and health source audit (2026-11-03)
- `run_paper_trading.py`: SOURCE-VERIFIED startup sequence is Redis -> EventBus -> ClickHouse repositories -> optional Neo4j -> exchange/universe -> system build -> trade-state persistence initialize/restore -> module initialization -> restored-position resubscription -> learning worker/agents/recorders -> health server -> scan loop. No runtime execution was performed.
- `aitos/app.py::initialize_all()`: initializes core modules sequentially, subscribes trade-lifecycle price handlers, then polls health for up to 5 seconds. It accepts both HEALTHY and DEGRADED as initialized.
- `aitos/app.py::shutdown_all()`: cancels price-feed subscriptions and shuts down core modules in reverse order.
- `run_paper_trading.py`: its main loop `try/finally` starts after substantial startup work. An exception during earlier startup can bypass the normal finalizer (AITOS-ENG-005).
- HealthServer monitors core modules plus auxiliary recorder/MarketOS/agent modules, while the periodic self-heal loop iterates only core `components.all_modules()` (AITOS-ENG-006).
- `/health` returns HTTP 200 for DEGRADED modules by design; consumers must parse the JSON status (AITOS-ENG-007).
- `aitos/telemetry/position_lifecycle.py` stores only the latest 2,000 events in process memory. It is not a durable audit log (AITOS-ENG-008).
- `aitos/forensics/data_flow_registry.py` stores latency samples in process memory (deque maxlen 2,048 per stage/symbol bucket); restart clears the percentile window. Its `(stage, symbol)` bucket dictionary has no TTL/eviction in the inspected source; memory impact under dynamic-universe symbol churn is NOT MEASURED (AITOS-ENG-009).

## Inventory update template
For each concrete module add:
- Import path and owning package.
- Entrypoint/initializer and shutdown hook.
- Direct dependencies and event contracts.
- Configuration keys and defaults.
- State read/written and persistence owner.
- Consumers/callers and downstream effects.
- Tests (unit/integration/contract).
- Metrics/logs/traces.
- Paper/live impact.
- Startup/readiness and restart behavior.
- Evidence links, test command, runtime window.
- Status and unresolved questions.

Do not infer a module's health from its name, file existence, class implementation, or a successful process start.

## Added validation assets — 2026-11-06

- `tests/test_clickhouse_revision_hazards.py`: eight integration-marked temporary
  embedded-engine counterexamples against the existing persistence schema/readers;
  passing means hazards reproduced, not safety certification. Never targets a
  deployed database; forced merges are confined to pytest temporary directories.
- `.github/workflows/persistence-quality.yml`: relevant PR/manual isolated SQL
  verification, pinned optional test engine, read-only repository permission and
  no migration/deployment/credentials. Remote execution NOT TESTED.
- No production module added or changed by this increment. Proposed revision
  authority remains unimplemented pending the approval gate in
  `PERSISTENCE_REVISION_GATE_2026-11-06.md`.

## Approved unwired prototype — 2026-11-06

- `aitos/trading/revision_journal.py`: stdlib-only, explicit standalone temporary-
  file paper/backtest revision authority; scoped CAS/fencing/events/head/outbox.
  No app imports, default paths, live scope, config wiring or active projector.
- `tests/test_revision_journal.py`: 37 isolated unit/process tests, including two-
  process CAS/ownership races and abrupt process-exit transaction recovery.
- `tests/test_revision_journal_projection.py`: two optional embedded append-only
  evidence projection checks; no remote database/venue calls.
- `.github/workflows/persistence-quality.yml`: extended relevant PR/manual checks;
  no migration/deployment/credentials; remote execution NOT TESTED.
- Production binding, legacy import, complete financial validation, amendments,
  order-side fencing and capacity evidence remain separate gates.

## Typed paper recovery batch — 2026-11-06

- `aitos/models/trade.py`: existing snapshot serializer now includes updated_at;
  no trading/risk semantic change. Existing legacy decoder already reads it.
- `aitos/trading/revision_journal.py`: complete bounded transactional heads and
  point-in-time writer observation; not exchange-order authorization.
- `aitos/trading/paper_revision.py`: unwired paper typed codec, recovery snapshot,
  monitoring-prerequisite rehearsal and read-only bounded legacy assessment.
  No database importer, automatic worker, runner registration or order API.
- `tests/test_trade_revision_binding.py`: 36 new serializer/typed financial/
  recovery/monitoring/audit cases, including the actual simulated lifecycle.
- `engineering/tools/benchmark_paper_revision.py`: synthetic temporary-file local
  append/recovery benchmark, never an existing financial journal.
- Persistence workflow extended to these paths/tests; remote CI NOT TESTED.

## Immutable paper amendments — 2026-11-06

- `aitos/trading/financial_amendment.py`: stdlib-only strict command/metadata and
  pure allowed-delta fee/funding calculation; no DB, network, runner or venue calls.
- `aitos/trading/revision_journal.py`: explicit fenced `amend_closed`, immutable
  parent-linked payload extension, receipt dedup and complete replay validation;
  no automatic schema migration or active task loop.
- `aitos/trading/paper_revision.py`: explicit amendment adapter and known typed
  metadata support; ordinary post-close snapshot writes remain forbidden.
- `tests/test_financial_amendments.py`: 32 synthetic temporary-file cases including
  process races, abrupt exits, receipt conflict/corruption and lifecycle parity.
- Embedded projection test verifies original close, amended close and duplicate
  evidence retained after merge. Workflow extended, remote execution NOT TESTED.

## Addendum — paper projector / persistence console (2026-11-06)

| Component | Ownership / activation | Verification |
|---|---|---|
| trading/revision_projector.py | Explicit paper batch, injected async sink; no auto-start/DDL | 29 new unit/process cases; 3 new embedded cases |
| trading/revision_journal.py batch ACK/counts | Atomic scoped receipts and validated conservation | Included full backend 1917 pass |
| projection_evidence.py | Pure public whitelist/freshness contract | Backend/API/producer contract tests |
| console_api.py optional observer | Existing protected snapshot; unavailable by default | Real API/gateway/built Chromium |
| desktop PersistencePanel / projectionClient | Tenth read-only page; strict parsing and explicit examples | 20 new parser cases, ten-page/mobile smoke |

None installs a production runner or execution service. Full evidence and open
gates: `PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md`.

## Paper/native additions (2026-11-06)

- `trading/paper_shadow_runtime.py`: default-off runner mirror/managed worker,
  failure latch and thread-draining shutdown; no recovery authority cutover.
- `trading/account_reconciliation.py`: offline exact-decimal snapshot comparator/CLI.
- `native/windows_vault.py`: WinCred implementation; Linux fake tests verified,
  actual Windows API test skipped.
- `native/windows/`: native session manager, Inno installer/build/signing scripts.
- `engineering/tools/paper_service_soak.py`: dedicated-service harness, logic
  exercised through embedded SQL, actual real-service run NOT TESTED.
- Three new manual/validation workflows; protected signing requires external
  certificate/runner/review configuration. No signed binary produced.

See `WINDOWS_PAPER_INTEGRATION_2026-11-06.md` and pending independent review bundle.

## Finishing tranche — 2026-11-06

Added shared native console host, cross-platform TCP/thread tests, actual-runner-finalbody failure injection and shared Windows installed validation script. Real WinCred/signing/service verification remains pending. See `PAPER_NATIVE_HARDENING_2026-11-06.md`.

## Recorded account boundary — 2026-11-06

New offline Binance evidence adapter/CLI, shared strict file loader, additive schema-2 comparison and 41 regressions. Existing income reconciler and exchange fetcher unchanged; real provider validation pending. See `BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md`.

## Hosted validation handoff

Windows vault/manager/installer and real service harness are now exercised on hosted environments, no longer just prepared. Thin environment dependency recipes corrected; actual independent review #247 and trusted signing remain gates. See `REMOTE_VALIDATION_EVIDENCE.md`.
