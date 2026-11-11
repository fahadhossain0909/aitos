# External completion gates — no secrets in Git or chat

This is an actionable handoff, not independent approval or release authorization.
The founder stated a reviewer will be nominated and trusted signing infrastructure
is available. Those statements are preserved; they do not identify a reviewer or
make that infrastructure accessible to this sandbox.

## 1. Independent review

- Nominate an actual reviewer by GitHub username in issue
  https://github.com/fahadhossain0909/aitos/issues/247 .
- Review baseline `6f8052bbc9818bda83f6a566cd70aac82ccb4c46` to source
  `58807516c519372bb8b33a8c94caf421cd313de3`, including advanced main and accumulated changes,
  not only the two-file latest fix. See `PAPER_NATIVE_REVIEW_BUNDLE.md`.
- Record reviewer identity, exact reviewed SHA, findings and retest evidence.
- New code after that SHA requires review of its delta. Merely copying a SHA into
  signing input is not an approval attestation.
- No PR is needed to inspect this branch. Do not open a PR until the existing
  PR-triggered VPS diagnostic risk is independently resolved without resuming VPS.

**Current status: BLOCKED.** Issue has no assigned reviewer or recorded approval.
The author cannot supply independent approval of its own work.

## 2. Connect the existing trusted signing infrastructure

The current workflow `.github/workflows/windows-console-sign.yml` requires:

- A dedicated Windows x64 self-hosted runner with labels
  `self-hosted`, `Windows`, `X64`, `aitos-signing`.
- Python 3.12, Node 22, Windows SDK `signtool.exe`, and Inno Setup 6 available as
  expected by `native/windows/build.ps1` and `sign.ps1`.
- An accessible signing certificate/private key or HSM installed in the runner
  user's `My` certificate store. Never export/upload a PFX/private key to this
  issue, repository, chat or test artifact. Do not use a general untrusted runner.
- A protected `windows-signing` GitHub environment, required human reviewers and
  appropriate source restrictions configured by the repository owner.
- Environment variable `AITOS_SIGNING_CERT_SHA1` containing only the public
  expected certificate thumbprint (40 hex characters, not the private key).
- Independent source review completed before any signing dispatch. The exact
  reviewed SHA must equal the selected workflow source SHA.

After these prerequisites are verified, signing must cover app, setup and generated
uninstaller, then `validate-install.ps1 -RequireSigned` must pass and the trusted
signer must match. A successful unsigned build is not a substitute. The existing
workflow is manual-only; its availability on a non-default branch must be checked,
not solved by merging to main or weakening protection.

**Current status: BLOCKED.** User reports infrastructure exists, but runner location
was not supplied; runner inventory API is 403. Visible repository environments
still lack `windows-signing`. No signing dispatch has occurred.

## 3. Provider/account evidence

- Identify exchange, account type and environment. Current adapter is deliberately
  limited to Binance USD-M **paper/testnet** records, not real-mode verification.
- Capture read-only provider responses outside this sandbox's restricted network:
  complete balances, positions, explicit ONE_WAY/HEDGE mode, server-time cut and
  all income pages for one bounded inclusive receipt window. Never include API
  keys, signatures, request headers, cookies or bearer tokens in evidence files.
- Preserve provider provenance and capture sequence in private operator evidence.
  A terminal page alone does not prove complete capture or an atomic account cut.
- Prepare the expected schema-2 ledger snapshot independently, with the same
  account alias, mode, cut, position mode and receipt window. Do not derive expected
  balances/positions/receipts from the observed response just to obtain equality.
- See `BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md` for the exact accepted contract.
  Logical account aliases may be used consistently instead of identifying IDs.
- Place the two private files in the ignored `data/` tree or approved private storage;
  provide paths, not their secrets. Use a local project environment to run:

```sh
python -m aitos.exchange.binance_account_evidence \
  data/private-evidence/expected-schema2.json \
  data/private-evidence/recorded-bundle.json
```

Exit codes: `0` matched, `2` mismatch, `3` unavailable. Files must be fresh; do not
rewrite an old capture's timestamps. Outputs are read-only, sanitized and never
supply trading authorization. Never relabel a real account `testnet` to bypass
schema restrictions. Real-mode support requires separate implementation/review.

**Current status: BLOCKED.** User reports capture has not been collected. No provider
account, independent ledger files or trusted provenance is available to verify.

## 4. Recovery-authority/HA gates

Legacy financial recovery remains authoritative and the optional mirror default-off.
Independent review, historical-state assessment and independently sourced account
reconciliation must precede authority cutover. No passing synthetic soak is itself
permission to migrate history or change execution authority. Replica/HA failure,
power-loss durability and representative capacity remain separate unexecuted tests.
Do not enable live trading, modify production configuration or issue real orders.
