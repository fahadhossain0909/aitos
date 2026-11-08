# Protective intent containment + Operator Console

Date: 2026-11-07 · branch: arena/ed127d35-aitos

## Delivery

VERIFIED: SL/TP requests now durably preclaim a scoped SQLite intent before the
POST boundary, retain accepted unfilled protection as PROTECTED, and acknowledge
application only after the lifecycle's durable protective-ID write. Lost POST
responses remain quarantined, not automatically resubmitted. Replacement
acknowledgement follows the final cancellation-state write. Legacy acceptance
requires an actual exchange ID and NEW status; missing IDs and FILLED responses
are not fabricated into an unfilled resting order.

VERIFIED: opt-in read-only `/console/v1/snapshot` and `/console/v1/history` routes
use long bearer tokens for attached systems, financial-field whitelisting,
finite JSON conversion and bounded parameterized history queries. Health/history
waits are bounded. Default standalone mode is unattached and has no synthetic
financial values. Existing health-only callers remain opt-out by default.

VERIFIED: React/TypeScript desktop-shaped frontend builds, with seven pages,
responsive custom styling, charts, context dropdowns, percentile selection,
search/filter/export, record dialogs and bounded local unapplied drafts. Runtime
and sample views stay distinct. Snapshot failure clears financial state. Polls
are sequential with cancellation and timeout. No mutation or credential routes.

## Evidence

- VERIFIED: current backend non-integration regression: **1630 passed,
  15 deselected in 70.60s**. Log: `tmp/backend-console-final.log`.
- VERIFIED: dedicated console/intent and protective/lifecycle targeted run:
  **72 passed in 0.81s**; four additional intent tests subsequently passed in
  **28 passed in 0.32s** intent-file run and the final broad run above.
- VERIFIED: frontend unit contracts: **13 passed**; TypeScript + production
  bundle build passed; npm audit **0 vulnerabilities** at verification time.
- VERIFIED: headless Chromium smoke: all seven routes, runtime/sample isolation,
  position detail + locked-live dialogs, Escape, 390px overflow and no uncaught
  JS errors. Screenshots: `tmp/console/{overview,runtime,mobile}.png`.
- VERIFIED: Black check on 11 changed Python files and git diff whitespace check.
- VERIFIED: local preview serves on port 5173 with same-origin adapter proxy;
  standalone adapter stays loopback on 8765. No trading runner started.

These are offline/local checks, not production execution or exchange evidence.
Browser startup initially failed on missing NSS/NSPR shared libraries; resolved
using libraries already shipped in the npm Chromium package. No host package
installation or exchange connection was performed.

## Readiness and review limits

NOT MEASURED: real ClickHouse history SQL/recovery and monotonic state revision
behavior; argMax timestamp ties remain a known concern. Parameter binding and
query bounds are unit-tested, not real-server-certified.

NOT MEASURED: open-algo inventory reconciliation, exchange/testnet protective
round-trip, production p95/p99/end-to-end latency, paper soak and independent
review. Unknown protective intents intentionally require operator reconciliation;
this change does not safely auto-clear or recover every open conditional order.

NOT MEASURED: native desktop installers, secure credential vault, operator
control-plane authentication, installer signing/update, broker certification,
distributed fencing, Windows ownership behavior and one-click runtime bootstrap.

VERIFIED: no live activation/orders, production configuration/strategy changes,
VPS deployment, migrations/deletion, branch switch, commit or push performed.
A comprehensive independent whole-codebase review has **not** been completed;
this is an execution/persistence/console-focused pass, not a quant-firm readiness
certificate. No subagents were spawned or represented as independent reviewers.

## Safety, next gates and rollback

The Vite proxy is for private local development only: it forwards a server-side
token and is not production operator authentication. Do not attach real financial
data to an unauthenticated public preview. Browser install scaffolding is not a
native installer and the service worker deliberately caches nothing.

Next: strict revision-based persistence and real-service restart evidence;
read-only open-algo reconciliation; isolated paper soak and latency capture;
secure account/vault + approved mutation design; native packaging and OS tests.

To disable console exposure, stop local preview/adapter and omit the opt-in
console token from health servers. Do not remove or clear unresolved SQLite
intents to bypass recovery. Execution-journal rollback needs coordinated offline
review of pending intents and persisted protective IDs; never delete journals.
