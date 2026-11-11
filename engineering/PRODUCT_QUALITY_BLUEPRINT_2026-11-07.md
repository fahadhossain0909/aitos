# AITOS — Evidence-led product quality blueprint

Date: 2026-11-07 · Local-first · No live activation authorization

## Product ambition

Build an excellent operator product across financial correctness, recovery,
security, usability, reproducibility and maintainability. “Number one” is an
ambition, not a verified ranking. Do not invent a competitor comparison, quality
percentage or performance score without a reproducible benchmark.

Quality is a set of independent gates. A beautiful frontend cannot compensate
for unsafe recovery; thousands of unit tests cannot compensate for an unverified
exchange contract. No weighted average should conceal a P0 blocker.

## What is implemented in this increment

### 1. Explainable observation engine

`aitos/console_readiness.py` evaluates ten evidence categories: runtime attachment,
module observations, freshness, stage sample coverage, stage counters, read-only
surface, persistence recovery, account/protection reconciliation, soak/latency,
and security/native distribution.

Outputs include the exact observation, status, priority and next safe action.
States are `passed`, `blocked`, `not_measured`; counts are not a quality score.
A healthy attached snapshot still yields **not_certified**, never live approval.
Real-service and operational gates cannot pass from self-declaration or historical
unit-test counts. The engine is deterministic and auditable, **not an LLM**.

### 2. Operational quality UI

The eighth page, **Quality & readiness**, displays actual adapter evidence even
when the financial design preview is selected. Gate details expand to explain
observations and actions. JSON evidence can be exported. No fake readiness
certificate is substituted for an offline or older adapter.

### 3. Offline protective-inventory analysis

`aitos/execution/protection_audit.py` compares scoped durable intents with an
explicitly complete, fresh caller-exported algo inventory. It checks client and
exchange identity, symbol, closing direction, order type, status, quantity,
trigger and one-way/hedge protection semantics. Duplicate identities and invalid
coverage fail closed. Missing/open/unowned/mismatched evidence stays explicit.

An `unapplied_match` is only a reconciliation candidate. It does not verify
current account positions, fills or export authenticity. The audit never
contacts an exchange, submits/cancels orders, clears SQLite quarantine, changes
capital ownership or authorizes resumption. Terminal child-fill reconciliation
and real-account evidence remain required.

### 4. Recovery correctness hardening

Open-state selection now considers the latest retained timestamp **before**
examining lifecycle state. This prevents an older open state from being selected
merely because a later closed state was filtered out before aggregation.
Conflicting retained latest payloads, corrupt records, identity mismatches and
missing ambiguity metadata block recovery rather than being silently skipped.

This is not a strict revision protocol: ReplacingMergeTree may already have
merged/discarded timestamp-tied versions; clock regressions, writer fencing and
ordered revisions need a separately reviewed storage design and real-service
verification. No migration or destructive mutation was run.

### 5. Frontend input boundary and history navigation

The client rejects malformed financial records, invalid directions, nonfinite
numbers, malformed stage/module responses, stale timestamps and writable-console
contracts. Malformed optional readiness is dropped rather than displayed.
Snapshot failures clear financial rows and open position details. Journal
requests are bounded, timed out, cancellable and paginated; search is explicitly
batch-local. Offset pagination is not advertised as a consistent ledger snapshot.
Unknown/empty P&L is not synthesized as a zero realized total.

### 6. Repeatable delivery checks

A new PR/manual frontend workflow runs locked dependency installation, contracts,
TypeScript/build and dependency audit. It contains no deployment, exchange
credentials, write permission or live runner launch. The workflow source is
implemented; a GitHub Actions run has **not** been observed in this session.

## Intelligent brain architecture — next design gates

The observation engine is the safe foundation, not a claim that a new high-context
model was selected or integrated. No separate subagent or external LLM inference
was used in the product runtime for this increment.

A future model-assisted analyst should have these isolated layers:

1. **Evidence collector:** typed snapshots, scoped positions/history, recovery
   reports and source timestamps. Treat imported documents as untrusted data.
2. **Retrieval/provenance:** citations to exact evidence IDs and hashes; scope by
   account/session; redact credentials and unnecessary personal information.
3. **Reasoning/proposal:** produce a structured hypothesis, supporting evidence,
   contradicting evidence, missing information and a recommended read-only check.
4. **Deterministic validator:** reject invented citations, stale evidence,
   unsupported financial assertions and schema violations. Model text cannot
   modify a safety gate or turn `not_measured` into `passed`.
5. **Human review:** show assumptions and costs before approval. Strategy/risk
   changes require separate explicit authorization and independent validation.
6. **Execution boundary:** no model-to-order tool path. Any future control plane
   needs authentication, audited authorization, idempotency and recovery ownership
   independent of model output.
7. **Evaluation:** dated offline tasks, hallucination/citation checks, abstention,
   regression cases, adversarial evidence, replay reproducibility, latency/cost
   and provider availability. Do not select a model using anecdotal confidence.

Provider choice, credentials, external data egress, costs and evaluation thresholds
remain design decisions. No provider adapter or model integration is claimed here.

## Measurable release gates, not fabricated current grades

| Dimension | Required evidence before release | Current status |
|---|---|---|
| Financial correctness | Reproducible fees/funding/fill/P&L reconciliation and ledger invariants | Offline tests exist; real-account certification pending |
| Recovery | Ordered revisions, crash/restart, duplicated events, conditional-child fills, account snapshots | Local fault contracts improved; real-service gates pending |
| Security | Operator authentication, scoped sessions, secret vault, TLS/origin review, threat-model tests | Console read-only bearer boundary; production gateway/vault pending |
| Performance | Dated p50/p95/p99 and loss/error windows on declared hardware/load | Stage display implemented; production targets/results NOT MEASURED |
| Frontend UX | Keyboard/screen-reader checks, responsive views, nonmisleading provenance, error/loading states | Browser smoke verified; comprehensive accessibility audit pending |
| Research intelligence | Reproducible experiments, holdout discipline, provenance/citation evaluation | Existing research surfaces; new LLM layer not implemented |
| Desktop delivery | Signed install/update/uninstall, secure bootstrap, OS restart tests | Web preview; native packaging pending |
| Maintainability | Typed contracts, CI, deterministic tests, bounded dependencies, review/rollback | New contracts and CI source added; independent review pending |

### Recommended sequence

1. Review/stage strict monotonic persistence revisions and writer ownership;
   validate against isolated ClickHouse before any operational activation.
2. Add read-only venue inventory collection and terminal child-fill integration;
   run exchange contract tests only in an explicitly approved environment.
3. Perform isolated paper soak/replay with declared hardware, event-loss/error
   budgets and trace-derived end-to-end latency distributions.
4. Design authenticated operator gateway, OS credential vault and account-switch
   control plane. Never reuse the public dev preview as production authentication.
5. Extend frontend with real equity/drawdown/price lineage and cursor/snapshot
   pagination; validate full keyboard/screen-reader behavior.
6. Evaluate the model-assisted analyst offline, with zero order permissions.
7. Package signed native installers and perform per-OS crash/update recovery.

No release gate is satisfied solely because a button exists or a process is alive.
