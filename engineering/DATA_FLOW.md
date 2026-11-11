# AITOS Data Flow and Event Contract Map

## Intended logical flow
```text
Exchange REST / WebSocket
  -> receive timestamp + source timestamp
  -> decode / parse
  -> normalize symbol, price, quantity and event type
  -> validate schema, sequence, timestamp and freshness
  -> publish to Redis Streams / Event Bus
  -> consumer group receives event
  -> handler processes and acknowledges
  -> market state / feature state updates
  -> broad universe scan and candidate ranking
  -> expensive candidate-specific intelligence
  -> strategy decision and risk validation
  -> order intent / paper execution
  -> fill and position persistence
  -> open-position monitoring
  -> HOLD / exit decision
  -> close attempt / confirmation
  -> final state reconciliation and outcome journal
```

This is a target investigation map. Confirm exact event names, payloads, modules, and ordering from the current source before relying on it.

## Required event envelope fields
Where supported by existing contracts, verify:
- Stable event ID and event type.
- Symbol/instrument and market.
- Exchange/source timestamp.
- Local receive timestamp (UTC).
- Publish and consume timestamps.
- Schema/version.
- Source adapter and transport.
- Sequence/update ID where the exchange provides one.
- Trace/correlation ID.
- Validation outcome and rejection reason.

Do not introduce a new envelope or break event compatibility without an approved migration plan.

## Stage-by-stage audit
| Boundary | Evidence to capture | Failure modes |
|---|---|---|
| Exchange -> receive | source time, receive time, connection/subscription, event count | connected-but-silent, wrong endpoint, stale stream |
| Receive -> parse | decode duration, parse errors, schema | malformed, blocking parse, dropped payload |
| Parse -> validate | freshness, sequence, duplicates, invalid values | stale/duplicate/out-of-order |
| Validate -> publish | publish count, duration, errors | silent publish failure |
| Publish -> consumer | stream lag, pending age, group progress | wrong group, stalled consumer |
| Consumer -> state/features | handler duration, errors, state version | handler routing, stale state |
| Features -> scanner | feature freshness, candidate count | duplicate compute, starvation |
| Scanner -> decision | candidate rank, reason codes, decision duration | stale candidate, wrong top-N |
| Decision -> execution | decision ID, risk result, request/ack/fill | rejected, timeout, duplicate |
| Execution -> position | stable position ID, fill quantity, persistence result | ghost/orphan position |
| Position -> exit | monitoring heartbeat, HOLD/exit reason | unmonitored position |
| Exit -> reconciliation | close attempt, fill/confirmation, final quantity | partial close mismatch, false closure |

## Source-verified instrumentation notes (2026-11-03)
- `OpportunityScanner.scan_symbol()` records scanner-stage input/output latency, processing errors and trace completion using a monotonic timer.
- `run_scan_and_trade_cycle()` logs candidate ranks/component scores, kernel decisions/rejections, risk/lifecycle rejection reasons and publishes a `decision.snapshot` event.
- Position evaluation/monitor/close lifecycle events are emitted by `aitos/intelligence/position_runtime.py`; the structured event contract is documented in `docs/POSITION_LIFECYCLE_TELEMETRY.md`.
- These are source facts only. Verify event delivery, persistence, trace continuity and stage latency at runtime before declaring the complete chain healthy.

## Freshness and ordering
- Preserve source timestamps and local receive timestamps separately.
- Detect clock skew; do not treat a future timestamp as fresh without validation.
- Distinguish no events, stale events, invalid events, and consumer backlog.
- Use exchange sequence/update IDs where applicable; do not invent ordering guarantees.
- REST fallback must not silently mask a broken WebSocket. Expose source and freshness in telemetry.
- Historical backfill must not block or starve the live path.

## Recovery
Test consumer restart, Redis restart, adapter reconnect, application restart, replay, pending-message recovery, and position-monitoring re-registration in isolated paper/test environments. Validate idempotency and final state.

## Addendum — optional paper evidence flow (2026-11-06)

Explicit run_once → validate current local writer and scoped pending history →
bounded sink lookup by revision/event identity → insert missing immutable events →
complete post-lookup verification → atomic local batch ACK → validated counts →
sanitized observation → authenticated existing snapshot → operator gateway →
Persistence & recovery page. The built Chromium harness verified this chain with
synthetic inputs and an injected test sink, without browser route mocking. Real
source sink queries were separately verified against embedded ClickHouse.

Timeout/lost response is ambiguous; next explicit run inspects evidence rather
than blindly reinserting. Takeover may leave remote duplicate immutable evidence
but cannot authorize stale local ACK. Unattached/stale/invalid/failure observations
clear counters/export; public observations omit payloads, account IDs and errors.
No production consumer, runner or execution flow changed. Full details:
`PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md`.

## Optional runner mirror flow (2026-11-06)

Existing lifecycle handler → successful legacy write → optional complete Trade
mirror → journal/outbox → managed projector → protected snapshot observer. Legacy
restore/portfolio/financial reads remain unchanged. Mirror failures latch unavailable
evidence, without changing exits/risk decisions; do not use it as recovery authority.
Shutdown drains in-flight local threads/projection. Windows vault secrets feed only
the local read-only gateway, not browser/env/argv. Offline account snapshots require
same scope/cut and exact decimal strings. Service restart harness is prepared, not
executed against real services. See `WINDOWS_PAPER_INTEGRATION_2026-11-06.md`.

## Finishing tranche — 2026-11-06

Shadow captures a detached nested Trade before waiting for lock/thread; event identity and stored body share that capture. Native host smoke exercises actual local auth/proxy/revocation with synthetic credentials; not Windows GUI or remote service proof. See `PAPER_NATIVE_HARDENING_2026-11-06.md`.

## Recorded account boundary — 2026-11-06

Recorded Binance-shaped balances/position risk/income pages → strict normalization → schema-2 scope/window/mode validator → exact comparator → sanitized CLI status. No provider request, receipt publication or financial mutation. See `BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md`.

## Hosted validation handoff

Actual ClickHouse insert → lost local ACK → stopped service/offline probe → retained-storage restart → replacement writer → verified no-blind-reinsert ACK → bounded 60/300-second soak passed. Actual Windows native host → gateway → synthetic backend authentication/revocation/install/uninstall also passed. Neither establishes HA/account certification. See `REMOTE_VALIDATION_EVIDENCE.md`.

## Sustained-write harness correction

Each corrected soak iteration appends a unique event → verifies remote evidence → inserts → ACKs exactly one row. Final remote count must equal ten recovered seeds plus every iteration. Old cycling-ID evidence is explicitly limited. See `REMOTE_VALIDATION_EVIDENCE.md`.

## Advanced-main source integration

Main durable SUBMITTING → venue lookup/ACK → startup orphan checks preserved. Opportunity-derived stable trade ID is also the exact client-ID suffix, retaining retry identity and orphan mapping. Actual-fill attribution retained; live ownership is persisted before optional telemetry. Isolated PostgreSQL ↔ ClickHouse reconciliation passed. Evidence/risks/rollback: `MAIN_INTEGRATION_REVIEW.md`.
