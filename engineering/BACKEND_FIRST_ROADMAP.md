# Backend-first delivery plan and desktop control blueprint

Date: 2026-11-07. Baseline: `origin/main` at `040fff9c` (full SHA recorded by Git).
Work branch: `arena/ed127d35-aitos`. Status: initial audit and bounded research fixes;
NOT a declaration of backend completion or production readiness.

## Mandate and safety

Founder-approved sequence: audit/correct the backend first, then build a professional,
interactive desktop application. Preserve current trading semantics and risk limits.
No live orders, production activation, VPS deployment, real credentials, destructive
migration or persistent user-data changes are part of this phase. Architecture-sensitive
activation and financial policy changes need a separate explicit approval.

## Rebaseline findings

Main has advanced since `6f8052bb`: PR #243 is now merged, as is PR #245. Continuous
intelligence, evidence-bound approval, experiment orchestration, rollout primitives,
development/holdout separation, liquidity filters and persisted mark freshness must be
reviewed as existing capabilities, not proposed again as missing implementations.
The checkout was shallow; fetching complete ancestry enabled a normal fast-forward
without switching branches, rewriting history or using unrelated-history merges.

Open work observed at audit time:
- PR #246: quantitative intelligence/statistical contracts, models, validation and
  reduce-only lifecycle overlay. This is NOT merged into this baseline. Coordinate
  before editing its scanner/statistics/lifecycle files; do not duplicate it.
- PR #244: paper testnet routing. Review endpoint, execution-mode and permission
  contracts separately; testnet is not equivalent to paper simulation.

## Source-backed capability/wiring matrix

VERIFIED here means source/call-path inspection, not exchange-connected certification.

| Capability | Existing owner / call path | Evidence and next gate |
|---|---|---|
| Paper/live orchestration | `run_paper_trading.py`, `run_live_trading.py`, `aitos/app.py` | VERIFIED composition roots; startup failure, teardown, restore/restart matrix still requires service-backed evidence |
| Market ingress and persistence | `aitos/market_data/*`, `CanonicalMarketDataPersistenceSink`, `MarketDataRepository` | VERIFIED bounded historical sink separated from ingress; queue loss, shutdown flush, ordering and replay fidelity need isolated runtime tests |
| Decision/outcome recording | `JournalSystem` -> `journal.decision_recorded` / `journal.outcome_attributed` -> `LearningExperienceRecorder`; recorder initialized in both runners | VERIFIED wiring; decision-less outcomes and management-decision evidence have recent coverage; verify durable completeness and duplicate delivery against real ClickHouse |
| Learning execution | `run_continual_learning.py` -> `ContinualLearningWorker.run_once/run_forever` -> checkpoint/cursor | VERIFIED entrypoint; restart consistency, ownership and late-adjustment handling need real-service evidence |
| Research analysis/drift | `ContinuousIntelligenceEngine.analyze_store/analyze`, `DriftDetector` | VERIFIED callable APIs; no automatic reporting schedule found in reviewed runners/worker; intentional offline boundary, not a proven runtime defect |
| Experiments | `ExperimentEngine.run/run_hypotheses`, external evaluator callback | VERIFIED library orchestration; callback is not intrinsically proof of canonical engine usage; executable canonical adapter and dataset/model provenance remain integration work |
| Candidate validation | `ContinualLearningPipeline` -> `CandidateValidator` -> `WalkForwardValidator` | VERIFIED wiring; frozen-strategy isolation repaired in this audit, metrics/statistics and fitted-model provenance need more review |
| Model governance | `ModelRegistry.promote`, `PromotionApproval` | VERIFIED explicit approval/digest workflow; it does not authenticate an operator or prove historical dataset/model identity |
| Rollout | `RolloutCoordinator`, injected canary/production/rollback callbacks | VERIFIED library primitive; no authenticated deployment adapters or periodic canary collector found in reviewed application entrypoints; deliberately NOT activated |
| Monitoring | `HealthServer`: `/health`, `/metrics`, `/data-flow`, `/position-lifecycle` | VERIFIED read-only diagnostics; no authentication/TLS in this server; recent lifecycle deque is NOT the durable trade ledger |
| Configuration | `config/settings.py`, `config/tuning.py`, `SystemComponents.reload_runtime_tuning` | VERIFIED typed settings and restart-bound checks; future control API must reuse these contracts rather than bypass them |
| Desktop control | no desktop package/control API in reviewed baseline | NOT IMPLEMENTED; plan below, no frontend fabricated in this phase |

## Delivery packages and acceptance gates

### B0 — Reproducible evidence (current package)
- Sync baseline, preserve changes, inventory overlapping PRs and real call paths.
- Reproduce research-isolation and experiment-identity defects before fixing.
- Add regression tests; run targeted and broader isolated suite.
- Keep runtime measurements explicitly NOT MEASURED.
Exit: evidence report, tracked findings, test commands/results and bounded diff.

### B1 — Financial state and execution correctness (next safety priority)
Trace signal -> capital reservation -> risk -> order -> ACK/fill -> durable state ->
monitor -> partial/complete exit -> cost adjustment. Build failure-injection matrix:
- ambiguous submit timeout followed by exchange lookup; no blind resubmit;
- duplicate and out-of-order fills; partial fills/cancels; reduce-only exits;
- process termination between order ACK and state persistence;
- restored position feed coverage and protection continuity;
- restart during capital switch and late fee/funding reconciliation;
- account/mode isolation and concurrent ownership.
Exit: deterministic fake-adapter contract tests plus isolated Redis/ClickHouse paper
restart evidence. No live account used. Uncertain order state must block unsafe new risk.

### B2 — Data truth and transactional ownership
Document schema/version/precision, ordering and identity for all market/ledger events.
Map authoritative versus derived stores: ClickHouse market/history, Redis hot transport,
SQLite reservations/calibration/switch journals, registry/checkpoint files. Audit each
transaction/retry/idempotency boundary before proposing an additional OLTP database.
Exercise gap/reconnect, consumer pending recovery, retention, disk-full, backup restore,
shutdown flush and data lineage. Distinguish best-effort history loss from order-state loss.
Exit: measured completeness/duplicates/gaps and tested restore procedures, RPO/RTO targets
agreed with founder. Any migration requires independent approval and rollback design.

### B3 — Research and quant certification
- Audit event-frequency Sharpe semantics before presenting annualized metrics.
- Validate cost/funding timing and passive queue model assumptions using recorded data.
- Run full baseline/ablation/cost replay; do not concatenate disjoint regimes and claim
  continuous-path performance. Regime slicing policy needs a dedicated design.
- Validate point-in-time feature availability, fitted-parameter lineage, overlap purge,
  embargo and holdout access discipline. Chronological splitting alone is not proof.
- Assess paired/dependence-aware bootstrap and repeated-experiment adjustment; coordinate
  with PR #246 rather than duplicating statistical validation.
- Track dataset/schema/code/model digests, seeds, execution assumptions, number of trials
  and acceptance-policy version. Current experiment hash is not complete provenance.
Exit: reproducible OOS report with uncertainty, side/regime coverage and no profitability
claim based only on sample count or synthetic unit tests.

### B4 — Latency and reliability certification
Use the existing `LATENCY_BASELINE.md` and telemetry, not a parallel framework.
Measure receive/decode/queue/features/decision/risk/submit/ACK/fill/persistence separately.
Include p50/p95/p99/max, samples, event age, throughput, backlog, drops, CPU/RSS, workload,
clock semantics, warm-up/outage windows and commit. Reserve resources for open-position
protection; keep learning/graph/report queries outside the critical execution path.
Exit: measured before/after load and failure tests with no correctness regression.
No blanket language rewrite or Redis tuning before evidence and approval.

### B5 — Backend control/read-model foundation
Specify authenticated local API/IPC and engine supervisor, durable trade queries,
bounded chart aggregates, capability discovery and typed settings metadata.
Read-only dashboard first; controls after command authorization and recovery tests.
Exit: API contract tests, mode/account isolation, audit log, rate/query limits,
secret redaction, command idempotency and reconnect/version-conflict handling.

### D1 — Desktop delivery (only after relevant backend gates)
Build native shell and application UX against B5 contracts, followed by supervised
engine packaging, signed updates and per-OS installer/recovery tests. Tauri + TypeScript
frontend + Python sidecar is a candidate, not a selected/deployed dependency.
Evaluate OS webview/Python/native service packaging before committing to the stack.
Docker-backed launcher may be an interim product; Docker-free installation needs a
separate architecture decision. Never silently substitute volatile storage.

## Desktop product contract (design, not implemented)

### Navigation and data
- Overview: equity, realized/unrealized/net PnL, drawdown, exposure, readiness and alerts.
- Positions/orders: open quantity, fees/funding, protection, monitoring age, order state.
- Trade history: durable paginated ledger, UTC/timezone choice, mode/account/venue filters,
  drill-down to entry/management/exit reasons and reconciled costs; bounded export.
- Research: reproducible backtests, ablations, cost scenarios, walk-forward/holdout evidence.
- Data/operations: feed gaps, consumer lag, persistence/learning freshness, stage latency.
- Connections: exchange capability discovery, masked account profiles, credential rotation.
- Settings: effective config, proposed diff, validation, restart-required warnings, history.
- Governance: evidence review, authenticated explicit approvals and model rollback status.

### Professional visual direction
Dense but legible workstation, not a promotional landing page: restrained graphite/navy
neutrals, one action accent, consistent spacing and typography, tabular numeric alignment,
accessible chart palettes and redundant status text/icons (not red/green alone).
PnL currency/units, cost basis, time window, data age and missing-data states stay visible.
Progressive disclosure and saved layouts reduce clutter. No decorative metrics or fake
live data; no gradient/card template in place of a functional information hierarchy.
Validate wireframes with founder workflows before implementing a visual system.

### Safe interactivity: not every internal value is editable

| Change class | UX/control contract |
|---|---|
| Layout, timezone, filters | local preference, no trading impact |
| Bounded reporting/backtest inputs | validated job request; isolated resource budget, cancellation and reproducible ID |
| Runtime-supported tuning | typed allowed fields, effective-config diff/version, authorization, audit, explicit apply |
| Restart-bound settings | preflight + controlled restart; continue protection/reconciliation; never silent partial apply |
| Risk/strategy semantics | privileged review/approval and validation evidence; no unrestricted sliders |
| Exchange/account/credentials | capability and permission check; reconcile positions/orders/reservations; switch guard; never move open exposure by relabeling |
| Paper/testnet/live | distinct sessions/data namespaces, visible persistent mode, explicit human authorization; no one-click escalation to live |
| Emergency actions | pause entries, cancel orders, reduce/flatten and stop engine are separate commands with explicit effects |

Secrets live in the existing provider boundary/OS credential store. UI receives references
and redacted metadata, never plaintext secret readback. Add authentication, origin/IPC
permissions and audit trails; loopback binding alone is not authentication.
Closing the window must not implicitly terminate protection. Supervisor behavior under
window close, OS suspend, crash, update and offline periods must be designed and tested.
Chart queries use bounded/downsampled analytical reads, not direct database access from UI.

## Approval and completion

Backend completion means evidence gates, not a feature count. Each package records exact
scope/tests, real runtime limitations, remaining findings and rollback. No unattended
promotion/real trading is authorized by this roadmap. Changes to trading policy or
architecture-sensitive production behavior are proposed separately to the founder.

## Persistence gate refinement — 2026-11-06

Actual embedded-engine evidence now proves timestamp/merge hazards, rather than
only hypothesizing them. See `PERSISTENCE_REVISION_GATE_2026-11-06.md`.
Next proposed deliverable, awaiting explicit approval: isolated single-host paper
revision-journal prototype, not active recovery wiring. Require transactional CAS,
trusted scoped keys, writer generations and state/event/outbox atomicity. Then
separately review execution ownership, import provenance, real-service recovery
and paper soak before any cutover. Integer versions or finer clocks alone do not
close this gate. No migration/deployment or live activation is authorized.

### Prototype checkpoint — 2026-11-06

Founder-approved unwired single-host revision prototype is implemented and tested;
see `REVISION_JOURNAL_PROTOTYPE_2026-11-06.md`. Next gate is independent review and
production Trade/monitoring/intent/amendment/import contract design, followed only
with new explicit approval by isolated paper wiring and real-service recovery/
soak tests. The current legacy storage path remains unchanged and un-certified.

### Typed recovery checkpoint — 2026-11-06

Typed paper binding and simulated lifecycle restart/close rehearsal are verified;
actual Trade snapshot timestamp omission is fixed. Complete recovery is bounded
and monitoring/intent/feed/ownership prerequisite evidence never grants order
permission. Read-only legacy assessment never approves an import. See the full
remaining-work ledger in PAPER_RECOVERY_COMPLETION_2026-11-06.md.
Next gates: reviewed immutable terminal amendments and execution ownership,
independent review, then separately reviewed projector/runner wiring and real-
service restart/soak. No automatic production migration/deployment/live approval.

### Financial amendment checkpoint — 2026-11-06

Isolated immutable paper fee/funding amendments are now locally VERIFIED; details
in FINANCIAL_AMENDMENTS_2026-11-06.md. Production ingestion must still establish
trusted source/account/currency/receipt provenance, precision, baseline-cost
reconciliation and independently reviewed execution ownership. Next is a bounded
isolated projector/runner integration contract plus restart/soak tests, not an
automatic deployment. Independent review and native secret delivery remain open.
