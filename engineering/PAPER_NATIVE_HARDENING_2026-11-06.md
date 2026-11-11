# Paper/native finishing tranche — verified changes and remaining external gates

## Scope

Continued on `arena/ed127d35-aitos` with the existing Windows x64,
harness-first and review-bundle-first decisions. Preserved the accumulated working
tree. No commit/push, remote workflow dispatch, service activation, production
cutover/migration, deployment, account access, venue order or risk/strategy/Redis
change. This tranche fixes reproducible implementation defects and strengthens
executable validation; it does not fabricate external approvals or signed builds.

## Root causes reproduced and fixed

### Mutable snapshot race — REPRODUCED / ROOT CAUSE CONFIRMED

The shadow worker derived event identity from one snapshot but passed the original
mutable Trade to a later SQLite thread. Mutation during that delay caused quantity
2 to be retained although the captured event represented quantity 1. A second
reproduction showed queued records also changing while awaiting the write lock,
including nested consensus data.

Fix: encode/validate and reconstruct a fully detached Trade before either lock or
thread boundary; bind lookup, event identity and persistence to that same detached
snapshot. Do not use the caller's mutable object inside the thread. Both regression
cases now retain the original quantity and nested data.

Evidence: `tmp/completion-hardening/pre-fix-snapshot.log` and
`pre-fix-queued-snapshot.log`, each with one failing reproduction before its fix.
This affected the default-off shadow implementation, not a claimed production
incident. Dual legacy/shadow writes are still not one distributed transaction;
legacy recovery remains authoritative.

### Repeated cancellation — REPRODUCED / ROOT CAUSE CONFIRMED

A second cancellation while draining the native write cancelled its asyncio task,
letting the record/stop barrier complete before the native operation. The prior
single-cancellation regression did not cover this.

Fix: repeated record cancellation keeps shielding/draining the IO task, then
re-raises cancellation. If global loop shutdown has already cancelled the inner
future itself, latch unavailable and exit rather than spin forever on a terminal
future. That exceptional storage outcome is unknown and must be inspected on
restart; this is not a guarantee that Python cancellation kills native threads.

Evidence: `tmp/completion-hardening/pre-fix-double-cancel.log` plus regressions for
repeated cancellation, stop barrier, and terminal-cancelled-future handling.

## Runner teardown

An outer finally now guarantees an attempt to stop the new shadow worker even if
an earlier legacy teardown step raises. The normal path still stops it before
closing shared clients; stop is idempotent. Five tests execute the **actual runner
finalbody AST**, without constructing a trading system, injecting failures in
heal-task, health, persistence and repository cleanup. This does not rewrite or
certify every legacy resource cleanup path. No subscriptions/risk decisions changed.

## Native gateway lifecycle and installed validation

- Extracted the existing GUI's thread/socket logic into shared `LocalConsoleHost`;
  the GUI now uses this controller rather than a duplicate implementation.
- Reserved IPv4 loopback socket, observable startup failure, retained stop-during-
  startup requests, bounded join, explicit closed state and cleared controller
  credential references after termination. Immutable Python copies are not
  guaranteed erased; same-user compromise remains outside the vault threat model.
- Actual local TCP tests exercise unauthenticated denial, login, protected cookie,
  server-side upstream bearer, no bearer in response, logout/revocation and thread
  cleanup. Five startup/stop iterations and invalid configuration paths are tested.
- Frozen `--gateway-self-test` runs the same synthetic TCP/auth exercise, preceded
  by native API/asset self-test. No real credential, exchange or trading runner.
- New `native/windows/validate-install.ps1` is shared by unsigned and protected
  signing workflows. It uses a unique CI temporary install path, bounded owned
  process waits, both frozen self-tests, uninstall and executable-removal checks.
- Signed validation verifies the **expected trusted signer** for setup, installed
  application and uninstaller before running them. Success evidence is emitted
  only after tests and uninstall pass, including installer SHA-256, source commit,
  signature-check status and explicit non-authority flags.

Linux actual TCP/thread tests pass. PowerShell execution, frozen Windows build,
GUI, real WinCred, signed installation and certificate-chain/TSA behavior are
**NOT TESTED here**. They are explicit executable CI gates, not reported passes.
The HTTP smoke checks API transport/auth, not full Windows browser/Tk rendering.

## Service evidence traceability

The guarded service harness now rejects unknown phases before network/directory
work. Reports include evidence schema, stage, UTC timestamp, validated source SHA
or `not_recorded`, and synthetic provenance. Connected phases query exact server
version; replacement recovery requires the version recorded at seeding. This is
verified in embedded SQL, not by an actual service restart. Offline probe does
not claim a currently observed server version. No new server/deployment was run.

## Final verification

| Check | Result |
|---|---|
| Full non-integration backend | **2004 passed, 37 deselected; 72.10s** |
| Embedded persistence / service harness | **21 passed; 2.88s** |
| Frontend | **84 passed**, production build PASS |
| Built authenticated browser | **1 passed; 13.70s** |
| Actual native Windows vault test | **1 skipped on Linux**, not a native pass |
| Cross-platform host tests without trading conftest | **12 passed** |
| Focused Black / F-lint / compile / Bandit / whitespace | PASS |
| Updated workflow YAML | Syntax PASS; remote execution NOT TESTED |

24 new backend cases are included in 2004, not additional. Existing embedded and
frontend totals overlap earlier reports. Latest logs are under
`tmp/completion-hardening/`. No production latency/capacity percentiles collected:
**NOT MEASURED**. No 24-hour/continuous-monitoring/HA claim.

## Actual completion ledger

| Area | Status after this tranche |
|---|---|
| Default-off runner shadow integration and lifecycle safety | Implemented, locally verified |
| Journal-authoritative recovery/financial cutover | Not implemented/activated; legacy authority retained |
| Exact account snapshot comparator/CLI | Implemented; real provider/account reconciliation NOT TESTED |
| Real-service restart/soak harness and evidence | Implemented, embedded logic verified; actual service/HA run NOT TESTED |
| Windows vault/native manager/installer/signing source | Implemented; actual Windows/signing validation NOT TESTED |
| Independent review | Bundle prepared; actual reviewer still pending |

The remaining external gates cannot honestly be completed by substituting mocks:
reviewed source must reach an approved Windows/service CI environment, a real
independent reviewer must inspect it, and a trusted certificate/signing runner must
be provisioned outside chat/Git. A signing SHA input alone is not review proof.
No private certificate, password, API key or GitHub token should be sent in chat.

## Rollback / next action

Keep the shadow flag at `0`; no existing service/data needs rollback. Scoped code
rollback must preserve journals, vault entries and unrelated working-tree changes;
never reset the repository or delete datasets. Native test installation only removes
its own temporary installed files. No user credentials/databases are uninstalled.

Next practical gate: independent review of the accumulated tracked **and untracked**
source, then unsigned Windows/native and dedicated-service validation. Only after
that, provision protected signing and run signed install validation. Full paper
recovery authority and production release remain separately reviewed gates.
