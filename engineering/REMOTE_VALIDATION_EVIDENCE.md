# Published source and actual hosted validation

## Current integration handoff

Current tested source is **58807516**: advanced main `e332e2b9` is fully included
on this arena branch, with individual conflict resolution. **Eight selected hosted
workflows PASS**, including actual PostgreSQL/ClickHouse reconciliation and Windows
unsigned build/install/uninstall. See [main integration review](MAIN_INTEGRATION_REVIEW.md)
for exact SHAs, fresh artifacts, local 2270-test result and remaining gates. Earlier
results below are historical and do not independently certify the merged source.

## Historical source: continuous-write coverage correction

Source `65644157531a888819e51553c160a4ac149977e2` corrects a P2 **test-harness
coverage gap**, not a production incident. The previous loop used `iterations %
100`: after 100 unique events it replayed existing events, so its hour-long PASS
must not be described as sustained new remote writes.

- **REPRODUCED / ROOT CAUSE CONFIRMED:** new deterministic 125-iteration embedded
  regression failed before the fix: 110 remote rows versus expected 135.
- **VERIFIED:** use unique IDs throughout the duration, require each projection
  batch to check/insert/ACK exactly one event, and verify final remote row count
  equals 10 recovered seed rows plus all soak iterations. New reports explicitly
  label `workload: continuous_unique_events`.
- **VERIFIED local:** 2045 backend passed / 38 integration deselected (74.70s);
  22 embedded SQL tests passed (6.60s), including normal wall-clock and accelerated
  125-event tests. The accelerated SQL test is not a wall-clock duration claim.
- **VERIFIED hosted:** all seven no-deployment workflows passed on corrected source
  `6564415`, and a separate 300-second continuous-write restart/recovery run passed
  (job 6m9s). See the pinned results below.
- Static checks: Black, existing Flake8 F-lint, compile and whitespace PASS. Ruff is
  not installed in the environment; no Ruff result is claimed.

No active trading/runtime path, financial authority, schema, risk policy or
production setting changed. Only the disposable harness and its test changed.

### Completed historical one-hour run — bounded/replay workload

**VERIFIED: PASS** on source `466b7465073eba8b23e1c7258ed06819278ba054`:
https://github.com/fahadhossain0909/aitos/actions/runs/42481976338

Total job 61 minutes, requested soak 3600 seconds. Artifact:
https://github.com/fahadhossain0909/aitos/actions/runs/42481976338/artifacts/13620215209
Archive digest: `sha256:286b43a401f24de5fe76cf84464d26f74287ea0c210506440b2ef19a18905e27`.
This passed the old bounded/replay invariants only. The later discovered 100-event
ceiling limits its interpretation; it is NOT sustained new-write/HA/capacity proof.
It does not count as an hour-long test of the corrected source.

## Corrected-source hosted validation

All rows below are **PASS**, source `65644157531a888819e51553c160a4ac149977e2`:

| Workflow | Evidence |
|---|---|
| Full backend / embedded persistence | https://github.com/fahadhossain0909/aitos/actions/runs/42511235702 |
| Windows native / build / install / uninstall | https://github.com/fahadhossain0909/aitos/actions/runs/42511235601 |
| Real ClickHouse restart / 60-second continuous-write soak | https://github.com/fahadhossain0909/aitos/actions/runs/42511235621 |
| Real Redis recovery | https://github.com/fahadhossain0909/aitos/actions/runs/42511235730 |
| Frontend | https://github.com/fahadhossain0909/aitos/actions/runs/42511235612 |
| Operator browser | https://github.com/fahadhossain0909/aitos/actions/runs/42511235642 |
| Embedded ledger SQL | https://github.com/fahadhossain0909/aitos/actions/runs/42511235584 |
| Additional 300-second continuous-write restart/recovery | https://github.com/fahadhossain0909/aitos/actions/runs/42511235316 |

[300-second corrected workload evidence](https://github.com/fahadhossain0909/aitos/actions/runs/42511235316/artifacts/13629726544),
archive size 142761 bytes; archive digest
`sha256:8a84f36321e7ef8e429199827a20914f016675ba643f0218ff6fefc427c3dea2`.
Successful hosted step outcomes and artifact metadata were observed, not a downloaded
payload or independent review. No exact remote iteration count is invented.

The corrected **3600-second continuous-write run completed PASS** on pre-integration
source `6564415`:
https://github.com/fahadhossain0909/aitos/actions/runs/42511298412
It is historical evidence for `6564415`, not an hour-long test of the integrated
source `58807516`. The completed GitHub job conclusion was observed explicitly.
Single-service synthetic workload is not replicated HA, production capacity,
representative p50/p95/p99 latency, power-loss or real-account certification.

## Executive summary

The founder explicitly approved continuing the remaining work. The accumulated
source was committed and pushed **only** to `arena/ed127d35-aitos`:

- `bad5f033f2cb036e6cd3679b4cc42842327993ad`: source snapshot, 229 files;
- `15771ebcf45d01bc88eba75795388da5f07f3159`: isolated CI dependency/diagnostic fix.

**VERIFIED:** all seven no-deployment validation workflows passed on the second
commit, including actual hosted Windows build/native vault/install/uninstall,
real disposable ClickHouse outage/restart/replacement-worker recovery, real Redis
recovery, full backend regression, embedded SQL, frontend and authenticated browser.
This supersedes earlier statements that those Windows/service checks were only
prepared. Earlier results remain historical, not retroactively claimed passes.

No main merge, VPS diagnostics/deployment, production migration, live order,
real-account credential access, risk/strategy/Redis tuning, trusted signing or
journal-authority activation occurred. Legacy financial recovery remains authoritative.

## Actual remote results (tested source SHA 15771eb...)

| Workflow | Result | Evidence |
|---|---|---|
| Full backend + focused recovery + embedded persistence | PASS | https://github.com/fahadhossain0909/aitos/actions/runs/42480562274 |
| Windows console/native vault/build/install | PASS | https://github.com/fahadhossain0909/aitos/actions/runs/42480562220 |
| Disposable ClickHouse restart + 60-second soak | PASS | https://github.com/fahadhossain0909/aitos/actions/runs/42480562186 |
| Real Redis crash/reclaim/retry | PASS | https://github.com/fahadhossain0909/aitos/actions/runs/42480562200 |
| Frontend contracts/build/audit | PASS | https://github.com/fahadhossain0909/aitos/actions/runs/42480562795 |
| Authenticated built Chromium/gateway | PASS | https://github.com/fahadhossain0909/aitos/actions/runs/42480562242 |
| Ledger analytics / embedded SQL | PASS | https://github.com/fahadhossain0909/aitos/actions/runs/42480562196 |

Local source baseline remains **2045 backend passed / 37 integration deselected**,
**84 frontend passed**, build PASS. Remote job/step outcomes are independently
observed through GitHub API/watch; exact remote counts are retained in the JUnit
artifact, not invented from inaccessible raw logs.

### Extended real-service run

A separate workflow dispatch requested **300 seconds** of synthetic soak on the
same tested source: https://github.com/fahadhossain0909/aitos/actions/runs/42480782238

**VERIFIED: PASS**, job duration 6m5s including setup/cleanup; the requested
300-second workload/recovery step completed successfully. Extended evidence:
https://github.com/fahadhossain0909/aitos/actions/runs/42480782238/artifacts/13617014935
(archive digest `sha256:2adb13e083a743334445ff8b4fc55618bd82b6cf1cacc6fc6bfdc99371a96355`).
The completed 60-second run also exercised an actual service stop,
refused-transport probe, restart with retained storage, generation takeover, stale
writer refusal and no blind reinsertion after lost ACK. Neither run establishes
replica/HA failover, 24-hour soak, power-loss durability or production capacity.

## Windows candidate available — UNSIGNED TEST SOFTWARE

[Download the Windows test artifact](https://github.com/fahadhossain0909/aitos/actions/runs/42480562220/artifacts/13617775438)

- Artifact: `UNSIGNED-Windows-console-test-only`.
- Artifact ID: `13617775438`; size: `16009539` bytes.
- GitHub **archive** digest:
  `sha256:08b43d46501d13b9c5b60ea3fb071b4bb44c4256e219a2de32de770ce62897b2`.
  This is not represented as the installer's individual file hash.
- Contains installer and installation evidence. Installer SHA-256 is recorded
  inside `install-evidence.json` by the actual Windows job.

Actual hosted `windows-2022` validation passed:
1. Shared native host tests with actual loopback TCP/auth/revocation.
2. Real WinCred unique synthetic-slot read/write/replace/delete (not Linux fake).
3. Node frontend build, PyInstaller frozen bundle and Inno Setup compilation.
4. Installation into a unique temporary directory.
5. Installed `--self-test` and `--gateway-self-test`.
6. Uninstall and executable-removal check.
7. Upload of unsigned installer/evidence.

This is not a trusted signed release or proof on every end-user Windows machine.
No Defender/SmartScreen bypass is recommended. The app is a local read-only session
manager opening the existing console in the browser; it does not launch trading.
Tk/browser rendering and long-running physical-machine compatibility still need
separate validation. Current-user vault limitations remain unchanged.

## Service/backend evidence artifacts

- [60-second service evidence](https://github.com/fahadhossain0909/aitos/actions/runs/42480562186/artifacts/13617197792):
  synthetic SQLite journal/manifests/stage reports, archive digest
  `sha256:5cc273bf09bbae866bd7b431fae2275d66f8dde2ae972025222a75dea3ead48f`.
- [Backend JUnit evidence](https://github.com/fahadhossain0909/aitos/actions/runs/42480562274/artifacts/13617221724):
  archive digest `sha256:32261227cfe66fd96a5c3ba8a16f17e78af6493162f1109826c174588f1c875b`.

Artifact metadata and successful job/step records were observed via GitHub API.
The sandbox cannot retrieve raw logs/artifact payloads redirected to the Actions
results host under its outbound allowlist; no restriction was bypassed. Artifacts
remain available through the authenticated GitHub links above. No claim is made
that their full binary contents were downloaded or independently reviewed here.

## Initial failures and implemented correction

The first Windows run (`42480298160`) failed at host test collection; the first
service run (`42480298161`) failed before seeding/creating its evidence directory.
The thin dependency recipes omitted `aiohttp`. A fresh minimal local virtualenv
reproduced the service import chain:
`aitos.trading.__init__ → lifecycle → execution → binance_executor → aiohttp`.
The native host also imports aiohttp unconditionally. This was not fixed by retries,
longer timeouts, exception swallowing or bypassing tests.

Correction: install the narrow pinned Windows packaging requirements **before**
host/native tests; install complete project/backtest dependencies for the service
harness's existing transitive trading imports. Added bounded failure annotations
and more specific installed-test errors. Second runs passed. Reproduction output:
`tmp/remote-validation/service-minimal-import.log`. Raw Windows failure traceback
was not accessible; observed failing step and matching import reproduction are
reported separately from full log inspection.

## Why no PR / no production side effects

Inspected all workflow push/PR triggers before publishing. Only seven explicitly
no-deployment workflows were enabled for this exact branch. Main/staging CD push
patterns do not match `arena/...`; no tags were pushed and auto-fix remains main-only.

Two existing main-target PR diagnostics can connect to VPS depending on a repo
variable. The integration cannot read that variable (403), so no PR was opened;
this avoids accidentally resuming prohibited remote diagnostics. No repo enable
flags, deployment environments or production settings were changed.

## Independent review and signing gates

Created actual review request **#247**:
https://github.com/fahadhossain0909/aitos/issues/247

The request links the exact source SHA, review bundle and real validation evidence.
It is a request/checklist, **not completed independent review**. Visible collaborators
listed only the repository owner, who is also the authenticated publishing actor;
no second reviewer or automatic independent approval was invented.

Visible environments were `papertrade`, `production`, `staging`; no
`windows-signing` environment was present. Self-hosted runner inventory access is
403. A trusted certificate/private key/HSM and protected signing runner are not
verified/configured for this workflow. Signing was **not dispatched**; no self-signed
certificate or fake trusted-release claim was substituted. Keys/passwords must be
provisioned outside chat/Git through the operator's approved secure mechanisms.

## Remaining completion gates

- Actual independent reviewer identity/findings/approval, beginning with #247.
- Protected Windows signing infrastructure, signed candidate validation and release approval.
- Trusted provider/account capture and reconciliation against independent ledger data.
- Reviewed journal-authority/financial cutover with legacy-history assessment.
- Replica/HA, longer workload/soak and production latency/capacity evidence where required.

Account adapter, paper mirror and installer remain read-only/default-off as designed.
No passing CI result supplies execution or production authorization by itself.

## Rollback / durable handoff

All source is preserved on the required branch; do not reset user data or Git.
Disable the branch-specific validation triggers if no further hosted tests are
wanted; do not change any production enable flag. The disposable test services
and temporary Windows installations are cleaned up by their own jobs. Uninstall
preserves user datasets/vault entries. No deployed production service needs rollback.

## Documentation-only handoff

The evidence/ledger handoff commit contains Markdown only and uses `[skip ci]`
to avoid rerunning identical jobs for documentation. Each validation claim above
is pinned to its stated tested source SHA, not an untested new code revision. No failed code validation is hidden by that documentation-only skip.

## Required external inputs — latest recheck

The founder states a reviewer will be nominated and trusted signing infrastructure
is available; provider capture is not yet collected. No reviewer username or runner
location was supplied. Issue #247 still has no assignee or approval; visible
environments still exclude `windows-signing`. No credential was solicited or used.
See [actionable external gates](EXTERNAL_COMPLETION_GATES.md) for exact safe next
steps. Neither review/signing nor provider verification/cutover has been completed.
