# Windows / paper integration implementation and verification

## Executive status

User selected **Windows x64**, **service harness/CI first**, and **review bundle
first**. Implemented on `arena/ed127d35-aitos`, baseline `6f8052b`; no commit/push,
merge, service activation, production migration, deployment, exchange request,
real order, risk/strategy change or Redis tuning/subscription change.

| Requested area | Delivered | Remaining gate |
|---|---|---|
| Active runner integration | Default-off opt-in paper shadow runtime, real lifecycle write path, managed worker, protected health/console observation | Legacy recovery remains authoritative; NOT a journal authority cutover |
| Real-service soak/failover | Manual dedicated ClickHouse restart/outage/replacement-worker/soak workflow and executable guarded harness | Actual service run NOT TESTED; replica/HA failover not implemented or certified |
| Account reconciliation | Exact-decimal, scope/currency/cut-bound read-only snapshot comparator and offline CLI | No provider credentials/fetch, account reconciliation run or receipt authenticity certification |
| Independent review | Concrete source/evidence/security checklist and review bundle | Actual independent reviewer NOT TESTED; author self-review is not independent review |
| Native vault / signed installer | WinCred implementation, native manager, per-user installer, unsigned validation and protected signing pipelines | Windows runtime/build/installation/signing NOT TESTED here; no signed artifact produced |

This is implemented code and harness preparation with verified local behavior,
**not a claim that all five production gates have been passed**.

## Runner integration and safety boundaries

`run_paper_trading.py` now calls `configured_shadow`; default flag `0` returns
without constructing a journal, acquiring ownership or starting a task. When
explicitly enabled it injects the existing repository async client, starts one
managed `paper-revision-shadow` worker and wraps lifecycle persistence writes.
No new Redis subscription, group, acknowledgement policy or concurrency setting.
No DDL, remote client creation or migration in the runtime.

Writes go to the existing legacy store first. Complete normalized Trade snapshots
then enter the isolated journal with content-derived immutable event identity,
current writer checks and CAS. Exact historical redelivery cannot move the head;
stale changed timestamps latch degraded shadow evidence. Timestamp ordering is
conservative, not proof of causality; clock rollback may require operator review.
Local `find_event` validates the event's history rather than querying private DB
internals from the adapter. Terminal snapshots remain retained after legacy delete.

Mirror failure latches unavailable evidence instead of publishing healthy counts.
It does not suppress legacy write failures, change entry/risk decisions, block
existing exit archival or disable monitoring. Shadow failure can therefore mean a
missing mirror event: **never treat this mirror as the recovery authority**. No
backfill or automatic import of existing legacy positions/history occurred.

The existing TradeStatePersistence recovery and financial reads stay legacy.
Console financial state and portfolio tracker continue to use that same store;
projection counts are separate evidence. HealthServer forwards the optional
observer to the existing protected snapshot route. No browser mutation/start API.

Startup rollback registers shadow shutdown on AsyncExitStack. Normal shutdown
cancels lifecycle subscriptions and drains shadow work before closing clients.
Cancellation of a record operation shields and drains its actual SQLite thread
before releasing the write lock; pending outbox survives, rather than promising a
final all-events flush. Stop disallows subsequent records and awaits the current
projection. SQLite validation remains O(history), so no shutdown deadline/SLO is
certified. Shutdown of the surrounding legacy application retains its existing
limitations; this is not a full process crash/power-loss guarantee.

### Opt-in configuration (not activated)

Keep `AITOS_PAPER_REVISION_SHADOW=0` for default behavior. To rehearse on a separately
approved isolated paper environment, provision the fixed prototype evidence table
outside the runner, and securely set:

- `AITOS_PAPER_REVISION_SHADOW=1`
- `AITOS_PAPER_REVISION_PATH`: absolute local SQLite path, parent already exists
- `AITOS_PAPER_REVISION_PROVIDER`, `AITOS_PAPER_REVISION_ACCOUNT`,
  `AITOS_PAPER_REVISION_RUN`: explicit stable scope identifiers
- `AITOS_PAPER_REVISION_WRITER`: explicit worker identifier
- `AITOS_PAPER_REVISION_EXPECTED_GENERATION`: exact known generation, `0` only for
  first acquisition. Restart does **not** silently steal ownership; inspect and
  explicitly choose the next expected generation.
- Existing `AITOS_CONSOLE_TOKEN`: at least 32 characters, backend only, never Vite
  or browser environment. Do not paste credentials in chat or source files.

No automatic enabling of this flag occurred. A full journal-authoritative recovery
cutover remains a separate design/reconciliation/review gate.

## Account snapshot reconciliation

`aitos/trading/account_reconciliation.py` validates exact recognized schema 1:
provider/account/mode (`paper` or `testnet`), common `as_of` cut, fresh aware
`observed_at`, explicit complete flag, bounded balance/position/receipt lists.
Balances include asset/total/available; positions symbol/side/quantity; receipts
receipt_id/currency/amount. Duplicate identities, unknown fields, bad cuts,
invalid balances, float/bool/exponent/nonfinite money and oversize inputs fail
closed. Values are decimal **strings**, up to 24 integer and 18 fractional digits;
no float conversion, epsilon matching, netting of opposite positions or mutation.
A one-unit change at the 18th fractional digit remains visible.

Both observations must be no more than 60 seconds old (future skew at most 5s),
with the same cut and scope. Result exposes only match/status and per-category
symmetric-difference record counts, not account IDs, receipt IDs or money. A
changed row can count as two symmetric-difference records. Both authority flags
are always false. Invalid/stale inputs produce unavailable, never a match.

Offline invocation:

```sh
python -m aitos.trading.account_reconciliation expected.json observed.json
```

Files are each limited to 4 MiB. Exit codes: `0` matched, `2` mismatch, `3`
unavailable; output is sanitized JSON. No networking, credentials, writes or
repair. A provider-supplied `complete` claim is **not** cryptographic authenticity
or independently verified pagination. No actual account was accessed/reconciled.

## Real-service harness / CI

`engineering/tools/paper_service_soak.py` requires explicit
`AITOS_SERVICE_TEST_ACK=isolated-test-services-only`, fixed IPv4 loopback and a
high port (default 18123). It creates only a fresh random `aitos_test_<uuid>` DB
with bound Identifier parameters and synthetic records; no DROP/reset of any DB.
The manual `paper-service-soak.yml` uses a dedicated ClickHouse 25.8 service:

1. Seed ten committed sink events, deliberately lose local ACK and retain pending.
2. Stop this job's container, probe refused transport, verify pending unchanged.
3. Restart the same container/storage; a replacement process explicitly takes the
   next generation, old writer is refused, existing receipts ACK without reinsertion.
4. Run bounded 1–3600 second repeated/idempotent workload, verify conservation and
   physical row counts, retain journal/manifests/reports as synthetic test artifacts.

Invariants use explicit runtime checks, not Python asserts that disappear under
`-O`. Manual dispatch does not publish a service or change production data.
**Actual Docker/service execution was not run**, per the selected harness-first
scope. Local embedded verification substitutes the driver and refused-transport
probe: it exercises SQL/data/state logic but not actual HTTP/admin Identifier
binding, Docker lifecycle, network failover, replication, power loss or long soak.
No 24-hour, HA, production latency or capacity claim.

## Windows vault and packaging

See `../native/windows/README.md` for detailed setup, threat model and gates.
Native WinCred API uses fixed namespace, bounded UTF-16 blobs, native pointer
freeing and no plaintext fallback. A fake injected API supports Linux tests; the
actual API is Windows-only. Current-user generic credentials are readable by
other same-user processes; immutable Python secret copies cannot be guaranteed
zeroized. Exchange-key management/provider credential wiring is not included.

The Tk Windows manager retrieves console password/backend bearer from WinCred,
reserves an ephemeral loopback socket, starts the authenticated gateway and opens
the existing console in the default browser. It starts **no trading runner** and
passes no secrets through argv, child env, browser storage or URL. It is a native
session manager with browser UI, **not an embedded WebView shell**.

Per-user Inno Setup installation needs no admin/service/firewall/startup changes.
Uninstall preserves user data and vault credentials. The unsigned workflow tests
real WinCred with a unique synthetic slot, builds, installs to a temporary path,
runs the frozen self-test and uninstalls. GUI/real-machine lifecycle needs further
validation. `windows-console-sign.yml` requires manual exact SHA selection, a
protected environment and trusted self-hosted Windows signing runner. Admin must
configure required reviewers/source restrictions and a certificate/HSM externally.
SHA input is not independent review proof. SignTool uses SHA-256/RFC3161 and exact
expected signer verification; Inno signing covers setup and uninstaller. No release
publication/updater, no private key in Git/chat, no protection bypass. A **signed
binary was not produced** and no Windows workflow was executed in this session.

## Verification evidence

| Check | Final local result |
|---|---|
| Full backend non-integration | **1980 passed, 37 deselected, 72.20s** |
| Embedded persistence + service harness logic | **21 passed, 2.85s** |
| Focused health/new functionality | **71 passed, 1.83s** (overlaps above) |
| Frontend | **84 passed**, production build PASS |
| Built authenticated Chromium | **1 passed, 10.07s** |
| Actual native WinCred test on Linux | **1 skipped**, NOT a Windows pass |
| Focused Black/F-lint/compile/whitespace | PASS |
| Bandit on new runtime/account/vault/launcher/harness | PASS, no suppressions added |
| Four affected workflow YAML files | Syntax PASS; remote execution NOT TESTED |

63 new backend cases are included in 1980; the new embedded harness case is
included in 21. No frontend code changed this tranche. Logs:
`tmp/windows-runner-final/{backend,embedded,focused,frontend,build,browser-auth,native-platform}.log`.
Actual Windows test invocation uses `python -m pytest` so repository imports are
available outside the broad `tests/` directory.

Author review corrected cancellation/thread-draining and startup-close races,
removed optimized-away harness assertions, and removed two unused health locals
while retaining the existing awaited rate-limiter check and health semantics.
This is not an independent review or a newly claimed production incident.
Review checklist: `PAPER_NATIVE_REVIEW_BUNDLE.md`.

## Unchanged / unverified areas

Market-data/event-bus health, candidate ranking, risk/strategy semantics and venue
execution are not recertified by this work. No real-account provider fetch, exact-
money migration, authenticated financial mutations, production runner authority
cutover, HA failover, continuous monitoring, native GUI certification or signed
release occurred. Representative p50/p95/p99/max and sustained-capacity window are
**NOT MEASURED**; test durations are not runtime latency measurements.

## Rollback and next gate

Flag `0` restores the original runner path; stop any explicitly opted-in shadow
normally before closing its client. Preserve journals and evidence. No production
schema change needs rollback. Remove only reviewed scoped changes if reverting;
never reset the broad working tree or delete user datasets/vault entries.

Next: independently review the accumulated diff plus untracked files, then run
Windows unsigned/native validation and the dedicated real-service workflow on
reviewed source. Provision protected signing only after review. Journal authority
cutover, provider/account certification and release publication need their own
successful evidence and explicit authorization. No push/remote workflow dispatch
was performed to imply those gates had passed.
