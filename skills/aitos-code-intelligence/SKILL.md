---
name: aitos-code-intelligence
description: External code-intelligence and forensic-audit orchestration skill for AITOS. Use this skill when investigating bugs, tracing data flow, understanding architecture, auditing strategy/trade lifecycle, analyzing runtime-vs-static mismatches, or preparing safe code changes. The external analysis engines are NOT AITOS dependencies; use them as read-only analysis tools when available.
---

# AITOS Code Intelligence & Forensic Audit Skill

## Purpose

Use external code-intelligence engines to understand and audit the AITOS repository without adding those engines as runtime dependencies to AITOS.

The central rule is:

> **Static analysis explains what the code can do. Runtime forensic telemetry explains what actually happened. Do not treat static inference as runtime fact.**

This skill is intentionally lightweight: this file is the only AITOS-side integration. Heavy analysis engines should run outside the AITOS runtime and be invoked by the AI agent through its available tools, MCP servers, local tooling, CI artifacts, or remote analysis environments.

---

## AITOS repository

Canonical repository:

- https://github.com/fahadhossain0909/aitos

AITOS is an event-driven Binance USDT-M Futures trading system. Its important investigation surfaces include market-data ingestion, Redis Streams/Event Bus, ClickHouse persistence, optional Neo4j graph storage, opportunity scanning, risk, strategy decisions, execution, positions, exits, journaling/learning, backtesting, and GitHub Actions forensic workflows.

Before changing code, inspect the current repository state and relevant workflow/telemetry implementation. Do not rely on historical assumptions if the current code disagrees.

---

# External analysis engines

These projects are external analysis references/tools. **Do not add them to AITOS requirements, Docker images, Compose services, or the trading runtime merely because this skill references them.**

## 1. RepoGraph — repository-level code graph

Repository:

- https://github.com/ozyyshr/RepoGraph

Use for:
- repository-level dependency understanding
- symbol relationships
- call/dependency graph exploration
- identifying relevant files before editing
- helping an agent understand a large codebase

Preferred use:
1. Build a repository map.
2. Locate the subsystem related to the reported symptom.
3. Trace callers/callees before making a hypothesis.
4. Use the graph to narrow the investigation surface.

Do not use graph output alone as proof that a runtime event occurred.

## 2. CALM — graph-verified codebase intelligence

Repository:

- https://github.com/Eilodon/CALM

Use for:
- real call graphs
- callers/callees
- edit context
- impact analysis
- hotspots
- dead-code investigation
- architecture boundaries
- tree-sitter based structural understanding
- MCP-oriented code intelligence

Preferred use:
- Use CALM when the question is "where does this function actually lead?", "who calls this?", "what breaks if I change this?", or "is this path dead/unreachable?".

## 3. Joern — deep static/data-flow analysis

Repository:

- https://github.com/joernio/joern

Use for:
- AST/CPG analysis
- control-flow relationships
- data-flow tracing
- call relationships
- source-to-sink investigation
- complex cross-function/static reasoning

Preferred use:
- Use when ordinary search is insufficient to explain how data moves through multiple functions/modules.
- Especially useful for market-data, Redis, execution, risk, and security-sensitive paths.

## 4. CodeQL — semantic program analysis

Repository:

- https://github.com/github/codeql

Use for:
- semantic bug hunting
- data-flow queries
- security-relevant paths
- suspicious source-to-sink behavior
- custom repository-specific queries

Preferred use:
- Use for high-confidence static findings and repeatable regression queries.
- Do not report a CodeQL match as an observed production incident until runtime evidence confirms it.

## 5. Semgrep — fast pattern/static analysis

Repository:

- https://github.com/semgrep/semgrep

Use for:
- bug patterns
- unsafe API usage
- error-handling mistakes
- duplicated/suspicious constructs
- custom AITOS rules
- quick repository-wide scans

Preferred use:
- Run early for broad static triage.
- Add a custom rule when a confirmed AITOS bug has a recognizable code pattern.

## 6. ast-grep — structural search and transformation

Repository:

- https://github.com/ast-grep/ast-grep

Use for:
- syntax-aware structural search
- finding equivalent code patterns
- locating repeated implementations
- safe mechanical refactoring candidates
- validating that a pattern is consistently fixed across the repository

Do not perform broad automatic rewrites without reviewing the resulting diff.

## 7. CodeBoarding — architecture/component mapping

Repository:

- https://github.com/CodeBoarding/CodeBoarding

Use for:
- component-level architecture understanding
- dependency/architecture visualization
- identifying subsystem boundaries
- documenting unfamiliar areas

Preferred use:
- Use at the beginning of a large audit to understand the subsystem boundary before drilling into individual functions.

## 8. Sourcegraph — large-codebase code intelligence reference

Repository:

- https://github.com/sourcegraph/sourcegraph

Use when available for:
- fast symbol/reference search
- cross-file code navigation
- large repository exploration
- understanding historical/reference relationships

Treat it as a search/navigation layer, not as runtime evidence.

---

# Recommended orchestration

When an AI agent receives an AITOS task, use this order unless the task is obviously narrower:

```text
User request
   |
   v
1. Define the exact symptom/question
   |
   v
2. Repository map / architecture context
   |       RepoGraph / CodeBoarding / Sourcegraph
   v
3. Symbol + caller/callee investigation
   |       CALM / RepoGraph / Sourcegraph
   v
4. Deep control/data-flow investigation
   |       Joern / CodeQL
   v
5. Broad static bug scan
   |       Semgrep / ast-grep / CodeQL
   v
6. Inspect AITOS forensic workflows + telemetry
   |
   v
7. Correlate static path with runtime evidence
   |
   +---- mismatch? ----> investigate first divergence
   |
   v
8. Root-cause hypothesis with evidence
   |
   v
9. Minimal code change
   |
   v
10. Regression test / forensic test
   |
   v
11. Re-run relevant AITOS audit workflow
   |
   v
12. Verify runtime/static agreement
```

The agent should stop and report a **telemetry gap** if the available evidence cannot distinguish competing hypotheses. Do not invent certainty.

---

# AITOS runtime/forensic evidence layer

The external tools should be connected conceptually to AITOS's existing forensic workflows and artifacts rather than replacing them.

Important AITOS workflow surfaces include:

- `.github/workflows/market-data-e2e-forensics.yml`
- `.github/workflows/market-data-contract-audit.yml`
- `.github/workflows/market-data-duplicate-forensics.yml`
- `.github/workflows/market-data-smoke.yml`
- `.github/workflows/market-data-throughput-benchmark.yml`
- `.github/workflows/orderbook-targeted-forensics.yml`
- `.github/workflows/production-market-data-forensics.yml`
- `.github/workflows/consumer-identity-forensics.yml`
- `.github/workflows/trade-path-diagnostic.yml`
- `.github/workflows/strategy-funnel-position-intelligence-audit.yml`
- `.github/workflows/strategy-trade-lifecycle-audit.yml`
- `.github/workflows/strategy-performance-audit.yml`
- `.github/workflows/lifecycle-runtime-reconcile.yml`
- `.github/workflows/root-cause-forensics.yml`
- `.github/workflows/production-audit.yml`
- `.github/workflows/runtime-hardening.yml`

These names are investigation entry points. Always inspect their current YAML and produced artifacts before assuming their exact commands, artifact names, or schemas.

---

# Core AITOS evidence chain

For market-data and strategy investigations, attempt to trace this logical chain:

```text
Exchange REST/WebSocket
        |
        v
transport / connection
        |
        v
raw event received
        |
        v
parser / normalizer
        |
        v
validated market event
        |
        v
Redis XADD / Event Bus publish
        |
        v
Redis consumer / consumer group
        |
        v
feature / indicator calculation
        |
        v
scanner / candidate
        |
        v
strategy evaluation
        |
        v
entry/hold/exit decision
        |
        v
risk approval
        |
        v
order submission
        |
        v
exchange acknowledgement/fill
        |
        v
position state
        |
        v
exit condition / exit decision
        |
        v
close/fill/reconciliation
        |
        v
journal / learning / telemetry
```

The investigation goal is to identify the **first point where expected behavior diverges from observed behavior**.

---

# Runtime evidence rules

When runtime telemetry is available, prefer evidence with:

- event timestamp
- correlation/request/decision/order/position ID where available
- symbol
- stage/component
- event type
- success/failure/degraded status
- reason code
- relevant metric values
- source timestamp and ingestion timestamp where available
- Redis stream/group/consumer information where relevant
- order/exchange IDs where relevant

For strategy investigations, preserve actual indicator/feature values when available, including relevant:

- CVD/delta
- order-book/liquidity measurements
- volume/volume-profile/VWAP values
- volatility
- funding/open-interest context
- market-structure state
- candidate score
- entry/hold/exit conditions
- risk decision
- position state

Do not replace these with generic statements such as "strategy was healthy" or "signal was weak" when exact values can be recovered.

---

# Static-vs-runtime correlation

Every serious investigation should classify findings as one or more of:

### Static fact
Directly established from source code, configuration, tests, or a reproducible static query.

### Runtime fact
Directly established from logs, metrics, telemetry, database state, exchange responses, or forensic artifacts.

### Correlated fact
A static path and runtime evidence agree on the same behavior.

### Hypothesis
Plausible but not yet proven.

### Telemetry gap
The current instrumentation cannot distinguish the competing explanations.

Never promote a hypothesis to a root cause merely because it looks plausible in the call graph.

---

# Investigation playbooks

## A. Market-data bug

1. Map the transport and ingestion modules.
2. Trace the target symbol from transport → parser → normalizer → Redis publisher.
3. Trace Redis stream → consumer group → downstream consumer.
4. Inspect connection/reconnect/error handling.
5. Run Semgrep/CodeQL for dropped exceptions, incorrect branches, stale cursors, and duplicated connections.
6. Use Joern/CALM for the exact data path when needed.
7. Compare every static stage with runtime receive/publish/consume telemetry.
8. Identify the first missing transition.
9. Fix only the proven failure point.
10. Add regression/forensic coverage for that transition.

## B. Strategy-entry bug

Trace:

```text
scanner
 -> candidate
 -> feature snapshot
 -> strategy evaluation
 -> decision
 -> risk
 -> order
 -> fill
 -> position
```

Capture the actual feature/indicator values and decision reasons. If a candidate disappeared between stages, find the exact filtering/risk/TTL/state transition responsible.

## C. Position/exit bug

Trace:

```text
position opened
 -> position state updates
 -> live market state
 -> exit evaluation
 -> exit reason
 -> order
 -> exchange acknowledgement/fill
 -> position close
 -> reconciliation
```

If a position remains open after an apparent SL/signal event, distinguish:

- signal generated but not acted on
- exit condition not actually satisfied
- risk/guard veto
- duplicate/stale position state
- order submission failure
- order rejected/cancelled
- partial fill
- exchange state not reconciled
- telemetry/reporting inconsistency

Never call an exit "executed" solely because an exit signal was generated.

## D. Redis/Event Bus issue

Investigate:

- producer connection lifecycle
- Redis connection pool usage
- `max_connections`
- XADD/XREADGROUP behavior
- consumer-group cursor/start position
- ACK/DLQ behavior
- stale messages
- duplicate consumers
- stream lag
- dropped/rejected events
- reconnect loops

Correlate Redis evidence with upstream receive timestamps and downstream processing timestamps.

## E. CI/CD or deployment bug

1. Inspect the workflow YAML actually used by the failing run.
2. Inspect the exact failing step/log.
3. Identify the code/config dependency of that step.
4. Use static analysis only where it helps explain the failure.
5. Avoid changing production behavior to fix a CI-only issue unless evidence requires it.
6. Re-run the smallest relevant workflow first.
7. Then run broader regression/audit workflows.

---

# Safe change protocol

When the user asks to change code:

1. Investigate first.
2. Establish the smallest root-cause-supported change.
3. Do not add RepoGraph/Joern/CALM/CodeQL/Semgrep/etc. to AITOS runtime dependencies unless the user explicitly requests that architectural change.
4. Preserve existing interfaces and telemetry unless the bug requires an instrumentation change.
5. If telemetry is insufficient, prefer adding focused telemetry rather than guessing.
6. Add or update a regression test where practical.
7. Inspect the diff for unrelated changes.
8. Run the narrowest relevant tests.
9. Run the relevant AITOS forensic workflow if available.
10. Report exactly what was changed, why, evidence, tests, and remaining uncertainty.

---

# When to add telemetry

Add telemetry only when source inspection plus existing artifacts cannot answer the question exactly.

Preferred event shape:

```json
{
  "event_id": "...",
  "timestamp": "...",
  "source_timestamp": "...",
  "symbol": "BTCUSDT",
  "stage": "market_data|feature|scanner|decision|risk|order|position|exit|reconcile",
  "event_type": "...",
  "correlation_id": "...",
  "decision_id": "...",
  "order_id": "...",
  "position_id": "...",
  "status": "success|rejected|failed|degraded|skipped",
  "reason": "...",
  "metrics": {}
}
```

Use the repository's existing logging/telemetry conventions when they already provide equivalent fields. Do not create parallel telemetry systems unnecessarily.

---

# Tool-selection matrix

| Question | Preferred tool(s) | Runtime evidence needed? |
|---|---|---|
| What is the architecture? | CodeBoarding, RepoGraph | No, but useful |
| Who calls this function? | CALM, RepoGraph, Sourcegraph | Usually no |
| What calls this function transitively? | CALM, RepoGraph | Usually no |
| Where does this value flow? | Joern, CodeQL | Often yes for runtime claims |
| Is this pattern buggy? | Semgrep, CodeQL | Depends |
| Where is this structural pattern repeated? | ast-grep | No |
| What breaks if I change this? | CALM, RepoGraph | Helpful |
| Why did the runtime fail? | Static tools + AITOS forensic telemetry | **Yes** |
| Why was a trade entered? | Static path + strategy audit artifacts | **Yes** |
| Why did a position remain open? | Static exit path + lifecycle/reconciliation artifacts | **Yes** |
| Did Redis lose/stall an event? | Static Redis path + stream/consumer telemetry | **Yes** |
| Is a fix safe? | Impact analysis + tests + forensic rerun | Preferably |

---

# Output standard for AI agents

For substantial AITOS investigations, structure the result as:

1. **Symptom** — exact user-reported problem.
2. **Affected subsystem** — concrete modules/files.
3. **Static path** — relevant callers/callees/data-flow.
4. **Runtime evidence** — exact telemetry/artifact evidence.
5. **First divergence** — first point where expected and observed behavior differ.
6. **Root cause** — only if sufficiently proven.
7. **Contributing factors** — separate from root cause.
8. **Fix** — minimal code/config change.
9. **Telemetry gap** — anything still unobservable.
10. **Tests** — tests executed/added.
11. **Forensic verification** — relevant workflow/artifact result.
12. **Remaining uncertainty** — explicit, never hidden.

When modifying code, include the exact files changed and keep the change narrowly scoped.

---

# Important architectural boundary

This skill is an **external intelligence layer**.

Do NOT:

- vendor these analysis repositories into AITOS
- add their dependencies to `requirements.txt` just to make this skill work
- add their servers to `docker-compose.yml`
- make the trading runtime depend on an external code-analysis engine
- slow live market-data processing for historical/code-analysis work
- claim runtime behavior from static graphs alone

The desired architecture is:

```text
             ChatGPT / Codex / other AI agent
                         |
                         v
              AITOS Code Intelligence Skill
                         |
          +--------------+---------------+
          |                              |
          v                              v
 External code-intelligence tools     AITOS evidence
 RepoGraph / CALM / Joern             CI artifacts
 CodeQL / Semgrep / ast-grep          forensic workflows
 CodeBoarding / Sourcegraph           logs / metrics / DB state
          |                              |
          +--------------+---------------+
                         |
                         v
               Evidence correlation
                         |
                         v
                 Root-cause analysis
                         |
                         v
                 Minimal AITOS change
                         |
                         v
                Test + forensic verify
```

This preserves AITOS as a lightweight trading system while allowing an AI coding agent to use much stronger external code-intelligence capabilities when available.
