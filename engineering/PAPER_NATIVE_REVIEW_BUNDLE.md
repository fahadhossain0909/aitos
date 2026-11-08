# Independent review bundle — pending actual reviewer

**NOT TESTED: independent review.** This bundle is preparation, not approval.
No separate reviewer/subagent was available or claimed. Work stays on
`arena/ed127d35-aitos`; no push, merge, deployment, signing or live activation.
User selected Windows x64, service harness/CI first, reviewer bundle first.

## Scope and review sequence

1. Review `WINDOWS_PAPER_INTEGRATION_2026-11-06.md` and prior projector evidence.
2. Inspect accumulated `git status`, tracked diff **and untracked source**. This
   branch contains substantial earlier work; a tracked diff alone is incomplete.
3. Review paper runner `configured_shadow` call, lifecycle store wrapper, health
   observer, startup rollback and normal shutdown ordering. Flag off must do
   nothing; no Redis group/subscription/tuning changes; no venue or risk changes.
4. Confirm **legacy recovery remains authoritative**. Shadow is not a cutover,
   execution fence, entry gate, historical backfill or reconciliation certificate.
5. Verify local writer CAS, exact historical redelivery, stale changed snapshot
   degradation, terminal preservation, failure latch and cancelled-thread draining.
   Model timestamps cannot establish causality; inspect tie/clock rollback limits.
6. Review account DTO completeness/cut/freshness checks, bounded exact decimal
   strings, identity/currency binding, duplicate rejection and sanitized outputs.
   Exact comparison is not provider authentication or trusted pagination.
7. Run service harness only against a new dedicated local disposable service.
   Inspect database namespace binding, localhost/high-port guard, manifest scope,
   offline probe, pre-ACK interruption, retained-volume service restart, stale
   writer refusal, no reinsertion and bounded soak. No HA/replica failover claim.
8. Inspect WinCred struct ABI and pointer freeing on Windows x64; all native error
   paths, secret-size/encoding limits, no fallback and same-user threat model.
9. Inspect launcher origins/port reservation, credential dialogs, no credential
   argv/env/browser storage, startup-close races and gateway thread cleanup.
10. Inspect pinned direct packaging dependencies and audit transitives; run actual
    frozen import/asset smoke, native GUI lifecycle, install/uninstall and Windows
    user isolation tests. No Windows validation result is claimed yet.
11. Verify signing runner isolation, required environment reviewers/source rules,
    private-key provisioning outside Git, SHA selection, SHA-256/timestamp policy,
    signed app/setup/uninstaller and expected-chain verification. SHA input is not
    review proof. Validate signed installation/uninstallation separately.
12. Confirm artifacts contain no secrets, `.env`, model/user account datasets or
    private signing material; no updater/autostart/trading permissions introduced.

## Evidence to rerun

```sh
.venv/bin/pytest tests -m 'not integration' --timeout=90 -q
.venv/bin/pytest tests/test_clickhouse_embedded_ledger.py tests/test_clickhouse_revision_hazards.py tests/test_revision_journal_projection.py tests/test_revision_projector_embedded.py tests/test_service_soak_embedded.py -m integration --timeout=90 -q
(cd desktop && npm test -- --run && npm run build)
AITOS_RUN_OPERATOR_BROWSER=1 .venv/bin/pytest tests/test_operator_gateway_browser.py -m integration --timeout=90 -q
# Windows actual API tests, outside broad trading test dependencies:
python -m pytest native/windows/test_native_vault.py -q
```

Manually dispatch service and Windows validation workflows on reviewed source;
**do not dispatch signing until independent review and signing infrastructure are
actually ready**. Preserve failing logs; do not relabel unexecuted tests as passed.
Record reviewer identity, exact SHA/diff, findings and retest evidence. Signing,
production cutover and release publication need their own explicit gates.

## Additional review targets

Review the reproduced mutable/nested snapshot race and repeated cancellation fixes
in `PAPER_NATIVE_HARDENING_2026-11-06.md`. Confirm legacy/mirror dual writes are not
misrepresented as atomic authority. Review shared native host cleanup, installed
`--gateway-self-test`, bounded temporary installation waits, setup/app/uninstaller
signer verification, and success evidence only after uninstall. Inspect source and
version provenance in service reports. Native/PowerShell/service/signing execution
and independent approval are still pending; do not replace them with Linux mocks.

## Recorded Binance account adapter review

Review `BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md` and the new offline adapter. Check
window/mode/schema binding, signed margin values, Decimal context independence,
receipt-kind identity, duplicate/terminal-page handling, zero/opposite positions
and strict JSON error sanitization. Structural page validity is NOT authenticated
provider completeness. Expected snapshot must come from an independent ledger,
not the observed adapter itself. Active fetcher/cursor/publisher are unchanged.
Actual provider/account authentication and independent review remain pending.
