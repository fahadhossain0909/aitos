# Paper projection and persistence console — completion evidence

## Executive summary

**VERIFIED:** the isolated paper revision prototype now has an explicitly invoked
outbox-to-sink projector, atomic batch acknowledgement, a sanitized optional API
observation, and the desktop's tenth page, **Persistence & recovery**. The built,
authenticated browser consumed an actual isolated projector observation through
the console API and operator gateway. All data in this harness is synthetic.

**This completes this bounded tranche, not the whole backend/frontend roadmap.**
There is no active projector service, production migration, execution-authority
cutover, exchange call, live activation or deployment. Strategy, risk and Redis
settings are unchanged. Work remains on `arena/ed127d35-aitos`, based on
`6f8052bbc9818bda83f6a566cd70aac82ccb4c46`; no commit or push was made. Existing
unrelated working-tree changes are preserved.

## Backend implementation

- `RevisionProjector.run_once()` requires an explicit paper journal, writer and
  injected sink. No automatic loop, client construction, credential lookup or DDL.
- Batch size defaults to 50 and is strictly limited to 1–100. Sink I/O timeout
  defaults to 6 seconds, configurable only within `(0, 30]`; this is a **per-I/O**
  timeout, not a total-attempt or SQLite full-history CPU deadline.
- Verify current writer, validate pending local history, inspect existing sink
  evidence, insert only missing events, read back the complete batch, then perform
  one transactional local batch ACK. Partial/unrelated/conflicting evidence must
  not cause acknowledgement. Check ownership again before publishing counts.
- Sink lookup includes revision keys **and event IDs**, finding event-ID conflicts
  even at another revision. All values use scalar typed SQL parameters. Only
  bounded numeric placeholder names enter the query template.
- Fixed prototype table; no schema modification. Read limits: 3 seconds server
  execution, 64 MiB read, 128 MiB server memory, 8 MiB result, throw-on-overflow;
  a 1001-row probe rejects evidence beyond the allowed bound. Insert and lookup
  require validated nonempty, bounded, single-scope batches.
- Timeouts and lost responses are ambiguous. Do not ACK or blindly reinsert;
  the next explicit attempt checks already persisted evidence first. Local writer
  fencing cannot cancel an in-flight remote write or fence venue execution.
- `acknowledge_batch()` validates every supplied event before updating any row in
  one transaction. Existing single-event acknowledgement delegates to it.
  `outbox_counts()` validates history/receipts before reporting conservation.
- The existing `/console/v1/snapshot` accepts an optional injected observation
  provider. Attaching it requires a bearer token of at least 32 characters.
  Default is unavailable. No new HTTP route or prototype import in active runner,
  health server or console API; only the pure observation validator is imported.
- Whitelisted schema-1 observations contain counts, batch counts and status, not
  payloads, account identifiers, raw exceptions, credentials or arbitrary extras.
  Timestamps must be timezone-aware; age over 60 seconds or future skew over
  5 seconds clears evidence. Counts must conserve and be safe integers.
  `live_authorized` and `production_certified` are always false.

## Frontend implementation

- A tenth navigation item and responsive **Persistence & recovery** workspace,
  consistent with the existing desktop design. Navigation numbering now handles
  page 10 correctly instead of displaying `010` or an obsolete nine-page range.
- Pending evidence, local acknowledgements and retained records; three explicit
  checkpoints; last-observation status and acknowledgement/authority caveats.
- Strict browser parser validates schema, types, valid calendar dates, freshness,
  conservation and complete batch acknowledgement. Malformed optional projection
  data is rejected without invalidating otherwise valid financial snapshot data.
- Unattached, failed, stale or malformed observations clear counters and disable
  observed export. A sample financial workspace never supplies persistence counts.
- An explicit **Explore synthetic example** action is separate from observed data.
  It has an unmistakable illustration banner and `synthetic: true` export wrapper;
  its illustrative source cannot pass as a runtime observation. Valid observed
  exports have `synthetic: false`, with both authority flags still false.
- No start-projector, account mutation, credential, migration or order controls.

## Module verification matrix

| Boundary | Actual evidence | Status |
|---|---|---|
| Local journal / batch ACK | Atomic rollback, history validation, retry and fencing tests | VERIFIED |
| Explicit projector | Fake-sink failure matrix and actual spawned-process exit/reopen | VERIFIED, isolated |
| Production-style sink adapter | Real embedded engine SELECT / insert / readback / conflict / reopen | VERIFIED, embedded only |
| Console observation | Auth, no-store, whitelist, stale/error clearing and producer contract tests | VERIFIED |
| Built UI through gateway | Actual isolated projector → API → gateway → authenticated Chromium | VERIFIED, synthetic inputs |
| Ten-page desktop | Unit tests, build, browser smoke, mobile checks and visual inspection | VERIFIED |
| Active production projector / runner cutover | Not installed or activated | NOT TESTED |
| Real remote-service restart / failover / sustained soak | No such environment run | NOT TESTED |
| Production capacity / latency percentiles | No representative workload or timing window | NOT MEASURED |
| Independent review / remote CI | Not performed during this tranche | NOT TESTED |

## Reproduced failure and supported root cause

**REPRODUCED / ROOT CAUSE CONFIRMED:** the first embedded projector tests failed
with ClickHouse Code 27 (`expected '('`) when nested `Array(Tuple(...))` query
parameters were serialized by the embedded binding. The nested values reached the
parser in a representation incompatible with the expected tuple literal. Direct
lookup preflight exposed the real error rather than relying on a sanitized
projector failure status. Replacing that binding with bounded scalar typed
parameters made the actual SELECT, conflict detection and restart tests pass.
This was a prototype implementation blocker, not a claimed production incident.

A focused Bandit scan exits 0 with **one explicitly reviewed B608 suppression**
on the structural SQL template: generated numeric placeholder names only; all
values remain typed bound parameters. The scanner emits a redundant-node warning.
This is not an unsuppressed-clean security scan or independent security review.

## Final local verification

| Check | Result |
|---|---|
| Full non-integration backend | **1917 passed, 36 deselected; 78.47s** |
| Embedded persistence suite | **20 passed; 1.71s** |
| Built authenticated Chromium harness | **1 passed; 9.46s** |
| Frontend unit suite | **84 passed** |
| Frontend production build | PASS |
| Ten-page browser smoke | PASS; persistence provenance, exports, malformed clearing, mobile, no JS errors |
| npm dependency audit | **0 vulnerabilities** at execution time |
| Focused Black / F-lint / compile / whitespace | PASS |
| Focused Bandit | Exit 0; reviewed B608 suppression as disclosed above |
| Two edited workflow YAML files | Syntax parsed; remote execution NOT TESTED |
| Active entrypoint import guards | PASS for root paper runner, health server and console API |

The 29 new non-integration projector cases are included in 1917, not additional.
The 3 new embedded cases are included in 20. The 20 new frontend parser cases are
included in 84. The auth browser harness also retains wrong-password rejection,
HttpOnly/SameSite cookie, no credential storage, logout, expiry, failed-revocation
warning/retry, CSP and mobile checks.

The actual spawned projector process exits with code 23 after the test sink has
fsynced its receipts but before local ACK. Reopening the journal verifies those
receipts and acknowledges without changing/reinserting the sink file. This test
uses a **durable test-only JSON sink**, not a remote ClickHouse service. Separate
embedded tests exercise the actual ClickHouse sink adapter. Neither proves
power-loss durability, remote failover or production exactly-once delivery.

### Reproduction commands

```sh
.venv/bin/pytest tests -m 'not integration' --timeout=90 -q
.venv/bin/pytest tests/test_clickhouse_embedded_ledger.py tests/test_clickhouse_revision_hazards.py tests/test_revision_journal_projection.py tests/test_revision_projector_embedded.py -m integration --timeout=90 -q
(cd desktop && npm test -- --run && npm run build && npm run test:browser && npm audit --audit-level=low)
AITOS_RUN_OPERATOR_BROWSER=1 .venv/bin/pytest tests/test_operator_gateway_browser.py -m integration --timeout=90 -q
```

Logs: `tmp/projector-final/{backend,embedded,auth-browser,bandit}.log` and
`tmp/console/projector-{frontend-tests,build,browser,audit}.log`.
Screenshots: `tmp/console/persistence-illustration.png`,
`tmp/console/persistence-mobile.png`, and
`tmp/console/projector-built-end-to-end.png`. The last is prominently watermarked
as an isolated paper test with synthetic data. These artifacts are not production
runtime evidence. Screenshots were visually inspected.

## Architecture and operational limitations

- Projection is at-least-once immutable evidence, not an execution fence or an
  exactly-once remote insert guarantee. Takeover during insertion can leave
  duplicate valid sink rows; it must not permit stale local ACK.
- Status describes the last bounded batch and local outbox. Empty outbox does not
  establish that historical sink rows still exist or certify full sink integrity.
- Journal integrity validation remains O(history), with synchronous SQLite work
  offloaded to threads. Cancellation does not kill an already running thread;
  storage outcome may need inspection on retry. No scale/SLO claim is made.
- Market data, Redis event flow, candidate/risk semantics and venue execution are
  not changed or recertified by these results. No new Prometheus metric family or
  high-cardinality labels were introduced; this is a local read-only observation.
- API-key storage, account/provider/currency reconciliation, exact-money accounting,
  authenticated mutations/jobs, native signing/install/update and LLM wiring are
  not completed by this read-only workspace.

## Remaining gates, in order

1. Independent review plus remote CI on the accumulated changes.
2. Separately approved opt-in paper-runner/execution ownership integration and
   real remote-service restart/soak/failover tests with retained evidence.
3. Provider receipt authenticity, account/currency reconciliation and financial
   precision review before any production financial authority claim.
4. Audited backend jobs/mutation contracts before interactive execution controls.
5. Native OS vault, signed installer/updater and clean-machine install validation.
6. Separate explicit approval and readiness evidence before production cutover or
   any live capability. No ranking, continuous-monitoring or 24-hour soak claim.

## Rollback

There is no production migration to reverse. Stop explicitly invoking this prototype
and omit the optional observation provider to restore unavailable observations.
Remove the persistence navigation/component/parser and the additive snapshot field
only through reviewed, scoped edits; preserve existing financial/API behavior.
Do not delete journal files, sink evidence or any user dataset, and do not reset
this broad working tree. Keep tests and logs as evidence. No deployed service or
production configuration needs rollback for this tranche.
