# Portable Desktop, Credential and Storage Architecture

Status: initial Linux native packaging foundation added to PR #248. This document
separates implemented behavior from future release gates.

## Desktop packaging decision

AITOS keeps one React console source tree and one Python gateway contract. Native
packages are generated per operating system and architecture because executable
formats, signing and OS key stores are not interchangeable.

- Windows: existing native session manager, Windows Credential Manager, unsigned
  EXE/installer validation workflow.
- Linux: Debian-family amd64/arm64 package, Tk session manager, Secret Service
  credential backend, local or SSH-forwarded backend port.
- macOS: not implemented; planned Keychain adapter and signed DMG/package.
- Tauri/WebView: not implemented. Current clients open the authenticated gateway
  in the system browser. Tauri remains a later shell replacement candidate, not
  a prerequisite for delivering a native Linux package.

The desktop package does not install, launch, or control the trading runtime.
Closing the manager shuts down its local gateway and revokes its local session.
The gateway remains read-only.

## Credential contract

`get_credential_vault()` selects an OS-native vault. Windows delegates to the
existing Credential Manager adapter. Linux requires a real Secret Service backend.
macOS uses the system keyring backend when packaged support is added. Unsupported,
locked, missing, or plaintext fallback backends fail closed. No credential is
written to config files, environment variables, browser storage, or command-line
arguments.

The Linux package uses the Secret Service integration provided by keyring and
requires an unlocked desktop login keyring. Headless VPS usage should use the
runtime service directly; a GUI desktop manager is not required on the VPS.

## Filesystem contract

The path resolver exposes:

- `AITOS_DATA_DIR`: durable application data root.
- `AITOS_CACHE_DIR`: disposable cache root.
- `AITOS_CONFIG_DIR`: user configuration root.
- `AITOS_LOG_DIR`: application log root.
- `MODEL_DATA_DIR`: explicit model storage override (existing behavior).

Defaults follow LOCALAPPDATA on Windows, Application Support/Caches on macOS,
and XDG data/cache/config conventions on Linux. Relative overrides resolve from
`AITOS_HOME`. Directory creation is explicit via
`ensure_application_directories()`.

Existing checkout-local `data/models` is retained when present, avoiding an
automatic data move during upgrade. Docker retains its existing `/models`
mount behavior. This is intentionally not an automatic migration of financial
journals or execution intent databases: safety-critical stores must retain explicit
paths and ownership until a separately reviewed migration/backup/recovery protocol
is approved.

## Linux build and acceptance

Build: `bash native/linux/build.sh` from repository root. CI sets
`PYTHON_BIN=/usr/bin/python3` so Tkinter and Python share the runner's system
interpreter. Output is an unsigned Debian package and SHA-256 manifest under
`artifacts/linux/`. It is a test artifact, not a signed public release.

The workflow validates package build, metadata, payload and checksum. It does not
yet install the package into a disposable graphical Linux session or verify real
GNOME Keyring/KWallet interaction. Those remain explicit acceptance gates.

## Remaining work

1. Run the Linux package workflow and fix any packaging or test failures.
2. Add a disposable Debian/Ubuntu install/uninstall test and a Secret Service
   integration test under a real D-Bus session.
3. Add AppImage and RPM packaging only after the Debian package is stable.
4. Replace the duplicated Tk/browser shells with a shared embedded Tauri shell
   only after a threat-model and memory/CPU comparison.
5. Add macOS Keychain, DMG signing/notarization and physical-machine acceptance.
6. Add endpoint discovery/capability negotiation for multiple runtime hosts;
   keep authorization, financial authority and safety limits strict and explicit.
7. Define explicit durable paths for each SQLite journal/intent store. Do not
   move financial state to cache or RAM, and do not silently relocate existing
   databases.
8. Add backup/restore, free-space checks, permissions, atomic writes, schema
   versioning and restart recovery before automatic state migration.

## Implementation checkpoint: 2026-11-06 (PR #248)

Phase 1's obsolete fresh-install model-directory test now follows portable user
storage. Phase 2 adds explicit canonical runtime origins (HTTPS except loopback),
origin-hashed credential slots, pinned native Secret Service/Keychain backends,
and authenticated read-only `/console/v1/capabilities` protocol 1.0. React now
fails closed against older runtimes without this contract: deploy compatible
read-only backend and frontend together; no silent fallback/endpoint discovery.
Native launchers accept explicit origins, not arbitrary paths or remote HTTP.
No runtime launch, live authority or credential access is added to JavaScript.

`aitos/config/durable_files.py` is an **opt-in foundation**, not a migration of
financial stores: private POSIX directory checks, free-space checks, fsync/atomic
JSON replacement, process locks and no-clobber SQLite snapshot staging. Financial
stores are not redirected and restore never activates a staged database.
Windows ACL validation, parent/symlink race hardening, versioned migration,
active recovery and concurrent migration acceptance remain outstanding.

An initial Tauri v2 shell and shared frozen Python gateway are under
`desktop/src-tauri` and `native/shared`. The shell embeds only its loopback
read-only gateway and permits same-origin navigation; no JavaScript IPC/control
capabilities are granted. Native dialogs and OS vault handle credentials; only
an origin is sent through child stdout. Closing the shell terminates its gateway,
not any trading backend. Existing browser/Tk packages remain the accepted fallback.
**Rust compilation, bundled execution, install and desktop vault acceptance have
not yet been verified.** Local environment lacks Rust and access to its registry.
The new hosted workflow builds unsigned Windows NSIS, Linux AppImage/DEB and
macOS DMG candidates and runs synthetic gateway smoke, plus isolated Linux
Secret Service cross-process persistence. It publishes no release or updater and
has no VPS/deployment step. These are CI definitions, not successful evidence.
Fresh-user installation/reboot, signed/notarized builds, Windows/macOS real-vault
acceptance and Linux session/desktop integration still require evidence.

Verification at this checkpoint:
- Full non-integration backend: **2312 passed, 2 skipped, 38 deselected** (79.64s).
- React production build: PASS; **93 tests across 8 files: PASS**.
- Operator browser integration: **1 passed** (9.61s).
- Shared source gateway self-test: PASS (synthetic backend/auth/revocation).
- New Python files F-lint: PASS; workflow/config parsing: PASS.
- Hosted native packaging/signing and provider/account evidence: NOT VERIFIED.

Real account equity/margin timelines, mark-price PnL, protection-order monitoring,
alerts delivery, consistent historical cursors, audited multi-user control,
secure remote updates and full backup/restore authority remain unimplemented.
The capabilities response explicitly marks these unavailable. Existing performance
is bounded-window, not all-time. No production certification, independent approval,
financial migration, trading activation or VPS deployment is implied.

Rollback: pair prior frontend/backend protocol versions, keep legacy launchers,
and do not activate staged snapshots. Never remove durable user data or substitute
memory/cache storage on failure.

### Follow-up implementation and evidence

Hosted run [42686896943](https://github.com/fahadhossain0909/aitos/actions/runs/42686896943)
at source `7df63472`:
- VERIFIED — Windows NSIS build, frozen gateway smoke and embedded executable smoke PASS.
- VERIFIED — Linux AppImage/DEB build and extracted AppImage gateway smoke PASS.
- VERIFIED — isolated Linux Secret Service cross-process persistence/delete PASS.
- NOT TESTED — macOS job cancelled; specific cancelled job was requested for rerun.
- NOT TESTED — installed-user desktop interaction/reboot, Windows real-vault test
  (newly added to workflow), macOS real-vault, signing/notarization.
These are unsigned synthetic gateway acceptance results, not trading health.

Phase 4 foundation now includes `aitos/exchange/operator_account.py`: explicit
injected GET-only signed transport, bounded account/position-risk reads, exact
Decimal-string totals and marks/unrealized PnL, provider-field allowlisting,
redacted errors, bounded wait and pending-request cancellation. No executor,
credentials, order submission, account selection or console route is wired.
Therefore account capabilities correctly remain unavailable. It does not collect
realized PnL, protection orders, historical account timeline or atomic cross-API
cuts. VERIFIED — fake-transport contract/validation/cancellation tests only.
NOT TESTED — real provider/account integration. Decimal values are not converted
to floats or silently replaced with zero after failure.

Phase 5 now has an offline backup utility with a versioned manifest, SHA-256,
byte size and SQLite user_version validation. It rejects incomplete/corrupt or
incompatible bundles, symlink bundle roots/files and existing destinations.
It retains failed/incomplete bundles for inspection and never edits runtime
configuration or activates recovery. Hashes detect corruption, not authenticity;
use trusted private offline directories. Parent/symlink race hardening and Windows
ACL validation remain outstanding, so this is not active production recovery.

Explicit commands (select owned offline paths; no automatic path discovery):

```sh
python -m aitos.config.backup backup --source /owned/database.sqlite3 --destination /owned/new-backup
python -m aitos.config.backup stage-restore --bundle /owned/new-backup --destination /owned/new-candidate.sqlite3 --expected-user-version 7
```

The version is an example, not the application's guaranteed schema version.
Review the selected database's actual version. Never activate a candidate against
running financial writers. No overwrite, deletion or live restore command exists.

VERIFIED — latest full backend regression: **2336 passed, 2 skipped, 38 deselected**
(80.72s); new backup/adapter/sidecar tests **24 passed**; new Python F-lint PASS.
REPRODUCED / ROOT CAUSE CONFIRMED — naive asyncio.gather returned on one transport
failure while a sibling request remained pending. Explicit task cancellation and
awaited cleanup now pass the regression. No runtime integration was activated.

Remaining acceptance: provider-to-console wiring, durable account timelines,
protective orders/alerts, consistent historical pagination, full multi-user
security review/control plane, authenticated secure updates, signed releases,
installed/fresh-user/reboot tests and independent review. The five-phase program
is **not complete**; these missing behaviors must not be inferred from the new
primitives or packaging smoke results.

### Account adapter HTTP wiring follow-up

VERIFIED — `HealthServer(console_account_observer=...)` now passes an explicitly
injected account observer into the authenticated read-only console. The fixed
GET `/console/v1/account` route is allowed by the session gateway, accepts no
query parameters/account selectors, requires attached-console bearer auth and
uses no-store responses. Missing/failed providers return 503 with no synthetic
balance and no raw provider exception. Route reads have a 12-second outer budget;
the adapter retains its 10-second request deadline/cleanup. No runtime default
constructs or activates a signed provider or loads exchange keys.

VERIFIED — adapter-to-HTTP synthetic integration, auth/no-query/no-write,
unavailable/error redaction and related gateway/API/health tests: **105 passed**
(2.93s). Real provider and UI integration: NOT TESTED / not enabled. The fixed
capability protocol still marks account timeline/margin/marks unavailable until
consumer integration and freshness evidence are provided; this internal route
alone does not claim complete account monitoring or change financial authority.

Explicit owner wiring is `HealthServer(..., console_token=...,
console_account_observer=BinanceOperatorAccount(signed_get))`. Supply a bounded
GET-only signed transport from an approved runtime owner, never from webview
input. Do not pass a trading executor/order client or enable live activity to test.
Rollback: omit the observer; account reads return unavailable and all existing
snapshot/history routes continue unchanged.

VERIFIED — full non-integration backend after HTTP wiring: **2341 passed,
2 skipped, 38 deselected** (81.03s); changed Python F-lint PASS.

### Account UI and durable timeline implementation

VERIFIED — Connections / Runtime view now mounts separate account observation
and account timeline panels. Sample mode never mounts the panels. Values remain
exact decimal strings; malformed, stale or unavailable observations are cleared,
not replaced with demo balances. Reads are cancellable/deadline-bounded and
nonoverlapping. Timeline refresh explicitly starts a new cut; older-page requests
use only a fixed same-origin route and signed continuation. These are bounded
observations, not provider-atomic cuts, realized returns or protection assurance.

`aitos/trading/account_timeline.py` adds an opt-in SQLite observation store with
explicit account/directory/key selection, private durable-directory preparation,
FULL synchronous commits, process write locks and no memory fallback. Only
allowlisted totals/timestamps persist. Owner/provider errors fail closed; no
trading store is relocated. Database/account identity is checked on reopen;
unknown existing databases are rejected, not migrated. UPDATE/DELETE triggers
protect against accidental history mutation (not a malicious local owner).

`RecordedAccountObserver(provider, timeline)` records successful reads before
returning them. Failed provider reads are not inserted. A cancelled request may
have already committed a monitoring observation; there is no automatic order or
financial-action retry. Collection is currently on demand from account reads,
not a supervised 24/7 polling service; do not infer gap-free coverage.

GET `/console/v1/account/timeline?limit=50&cursor=...` is bearer/session-protected,
no-store, bounded to 100 rows and has no account-selection or mutation parameters.
SQLite increasing IDs plus a pinned upper ID provide consistent **insert-only
account-observation** pagination. HMAC cursors bind store/account/key/page size;
new inserts cannot shift the cut. Reopen preserves rows/cursors with the same
key; key rotation invalidates cursors. Cursors use hashed scope, not raw account
labels. This does NOT solve existing ClickHouse trade-history offset pagination;
`ledger.consistent_cursor` remains false.

Example owner integration (not auto-activated):

```python
from aitos.trading.account_timeline import AccountTimeline, RecordedAccountObserver
from aitos.exchange.operator_account import BinanceOperatorAccount
# directory: explicitly owned durable directory, never install/cache/temp.
# cursor_key: 32+ secret bytes supplied by the runtime owner, never JavaScript.
timeline = AccountTimeline(directory, account=account_scope, cursor_key=cursor_key)
observer = RecordedAccountObserver(BinanceOperatorAccount(signed_get), timeline)
# Pass observer via HealthServer(console_account_observer=observer, ...).
```

Capabilities now report attached account margin/mark adapters and optional
observation timeline availability without asserting freshness/health. Their
existing null capability paths remain unchanged; UI uses fixed allowlisted
observation routes, never server-supplied arbitrary URLs. Production/live/control
flags remain false. Protection/alerts remain unavailable.

Evidence:
- VERIFIED — frontend production build; **105 tests / 10 files PASS**.
- VERIFIED — synthetic adapter -> SQLite -> authenticated gateway -> built
  Chromium observation/timeline UI, with exact decimals, cursor rejection and
  existing logout/expiry/revocation checks: **1 PASS (10.44s)**.
- VERIFIED — timeline reopen, account/key/page binding, concurrent-insert cut,
  stale/malformed rejection and mutation guards; HTTP/capability focused suite
  **24 PASS**.
- VERIFIED — full backend before final capability availability change:
  **2350 passed, 2 skipped, 38 deselected (81.42s)**.
- NOT TESTED — real signed provider, fresh-user install/reboot, Windows ACL and
  parent/symlink race acceptance, independent review and signing.

Rollback: omit the observer or use the unrecorded adapter; disable the panels by
sample view. Preserve the timeline directory and owner cursor key. Never delete
or replace financial databases as rollback. Backup/staged restore can inspect the
SQLite file offline, but there is no active recovery switch. Timeline corruption,
schema mismatch, disk/lock failure must surface unavailable, never memory fallback.

VERIFIED — final full backend including attached-account capability tests:
**2351 passed, 2 skipped, 38 deselected (79.83s)**; changed Python F-lint PASS.

### Protective-order evidence console implementation

VERIFIED — `ExportedProtectionObserver(capture, scope=...)` now adapts the existing
`audit_protection_inventory` into a scoped read-only console observation. The
owner supplies an async capture returning exactly `intents` and `evidence`; the
latter must declare scope/completeness/symbol coverage/current captured_at as
required by the existing audit. Capture deadline is 8 seconds, concurrent calls
fail closed, cancellation releases the lock and malformed/stale/cross-scope or
oversized captures fail unavailable. Capture is bounded to 4 MiB, 10,000 intents,
10,000 orders and 2,000 covered symbols. No new exchange IO or credentials exist.

Only allowlisted findings/counts leave the observer. Raw journal client IDs and
scope labels are not exposed; browser references are scoped SHA-256 prefixes.
`safe_to_resume` and `account_exposure_verified` are always false and automatic
order actions always empty. Matching open orders remain candidates, while absent
orders require terminal/child-fill evidence. No intent acknowledgement, replacement
order, quarantine clearing or execution/recovery state mutation is implemented.

`HealthServer(console_protection_observer=...)` passes the explicit observer into
GET `/console/v1/protection`, a fixed no-query gateway route requiring attached
console bearer/session authentication. Missing/failed providers return no-store
503, never synthetic zero counts. Endpoint deadline is 10 seconds. Connections /
Runtime view has a separate protective-evidence panel with captured timestamp,
counts and the first 100 bounded findings. Parser validates freshness, duplicate
references, exact finding/count agreement and absence of authority grants/actions.
Account/protection UI now also clears evidence at the freshness deadline, not just
on next poll or request failure. Sample view does not mount the monitor.

This is **caller-exported evidence comparison**, not authenticated live-provider
protection monitoring. Therefore `orders.protection` remains unavailable in the
full protection capability contract. `alerts.delivery` also remains false:
displayed local findings are not sent notifications, acknowledgements, or a
supervised alert service. No collection is enabled by default.

Owner example:

```python
from aitos.execution.operator_protection import ExportedProtectionObserver
observer = ExportedProtectionObserver(read_scoped_capture, scope=account_scope)
# HealthServer(..., console_token=token, console_protection_observer=observer)
```

Do not reuse an order executor as a capture provider. Audit/provenance acceptance
and actual signed account/order enumeration must be independently reviewed before
claiming live protection coverage. No missing-order interpretation establishes
absence of exposure or permits automatic resumption.

Verification:
- VERIFIED — targeted protection/audit/gateway tests: **89 passed** before added
  cancellation/size regressions; observer regression suite now **9 passed**.
- VERIFIED — final full backend: **2360 passed, 2 skipped, 38 deselected (79.80s)**.
- VERIFIED — frontend tests **110 passed / 11 files**; production build PASS.
- VERIFIED — built Chromium synthetic account/timeline/protection-to-gateway/UI
  flow plus auth/logout/expiry/revocation: **1 passed (10.13s)**. Test explicitly
  checks a missing-protection finding and absence of the raw client ID.
- VERIFIED — changed Python F-lint PASS.
- NOT TESTED — live signed exchange provenance, continuous collector, delivered
  alerts, consistent trade-ledger pagination, multi-user audited authorization,
  authenticated update installation, signed native releases, fresh-user reboot.

Rollback: omit the protection observer; the route returns unavailable. Existing
account/ledger stores and execution behavior are unchanged. Preserve all journals;
never delete intents or replace orders to clear a finding.
