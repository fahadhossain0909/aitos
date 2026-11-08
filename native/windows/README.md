# Windows x64 read-only console

## What exists

- Native Windows session manager (`launcher.py`) that opens the existing authenticated
  console in the default browser. **Not an embedded WebView and not a trading runner.**
- Current-user Windows Credential Manager storage via `CredWriteW` / `CredReadW` /
  `CredDeleteW`. No plaintext, environment or file fallback. Other same-user
  processes can access generic credentials; Python immutable strings are not
  guaranteed memory-zeroized. Do not use on a compromised/shared Windows account.
- Per-user, no-admin Inno Setup installer; no service, firewall, startup task or
  database installation. Closing the manager shuts down its local gateway/sessions.
- Unsigned Windows validation workflow and separately protected signing workflow.

## Local build (Windows x64, repository root)

Install Python 3.12 including Tk, Node 22.12+, and Inno Setup 6.3+ at its standard
location. Use an isolated Python environment. Then:

```powershell
./native/windows/build.ps1
```

The output under `artifacts/windows/` is **UNSIGNED TEST SOFTWARE** unless the
separate signing procedure was completed and signatures verified. Build caches and
artifacts are ignored. Do not copy `.env`, exchange keys or certificates into builds.

On first launch choose an operator password, existing local backend port and its
bearer token through masked native dialogs. Credentials are stored in slots
`AITOS/v1/console/operator-password` and
`AITOS/v1/console/backend-token/<port>`. Nothing starts the backend or trading engine.
The gateway binds a pre-reserved ephemeral IPv4 loopback socket; it never publishes
credentials to browser storage, command-line arguments or child-process env vars.
The browser still requires the operator login. An unavailable backend stays
unavailable; no sample data is promoted to observed financial evidence.

Use Windows Credential Manager to remove/reset these entries. Uninstall preserves
credentials and user data deliberately. No updater, exchange-key management UI,
account mutation API or live controls are included.

## Validation and signing gates

`windows-console.yml` runs the real native API roundtrip with a unique synthetic
slot, builds the frozen app and installer, installs into a temporary directory,
runs `AITOS-Console.exe --self-test`, then uninstalls the test installation.
The self-test validates native API availability, packaged assets and gateway import
without opening a window, reading real credentials or starting a server. It is not
GUI/clean-physical-machine certification.

`windows-console-sign.yml` is manual and requires an exact reviewed commit SHA,
a protected `windows-signing` environment and a trusted self-hosted Windows x64
runner labelled `aitos-signing`. An administrator must configure required reviewers
and source/branch restrictions **before enabling it**. The SHA input alone is not
proof of independent review. Do not expose this runner to untrusted PR code.

Provision a trusted signing certificate/private key or HSM in the runner's current-
user certificate store externally. Set the public thumbprint repository environment
variable `AITOS_SIGNING_CERT_SHA1`; never put a private key/PFX/password in the repo
or chat. Install SignTool on PATH. `sign.ps1` signs the app, configures Inno signing
of setup/uninstaller with SHA-256 and RFC3161 timestamping, verifies expected signer
and installer signature, and emits SHA-256 metadata. It does not publish a release
or auto-update users. Certificate selection uses a SHA-1 thumbprint, **not SHA-1
file signing**. Signing/TSA connectivity and clean-machine verification remain gates.

The signed installer authenticates its initial payload; the per-user installation
and same-user credential vault do not defend against a compromised Windows user.
Do not bypass Defender/SmartScreen. Reputation and enterprise policy acceptance
are not implied by a valid signature.

## Current evidence

Linux fake-transport vault tests pass; actual Windows test is skipped on Linux.
Windows CI, frozen build/install/GUI, real WinCred roundtrip and signing have **NOT
BEEN EXECUTED** in this session. There is **no signed release binary** to install.
See `../../engineering/WINDOWS_PAPER_INTEGRATION_2026-11-06.md` and review bundle.

## Installed-network validation hardening

GUI and frozen tests now share `aitos.native.console_host.LocalConsoleHost`. Its
actual loopback/auth/revocation and startup/stop tests pass on Linux; this is not
a Windows result. `--gateway-self-test` exercises that path with generated synthetic
credentials, no trading runner, no real vault entries.

Both workflows use `validate-install.ps1`: unique CI temporary installation, bounded
owned process waits, frozen import/assets smoke, frozen network/auth smoke, uninstall
and removal check. `-RequireSigned` verifies setup, application and uninstaller
against the configured trusted signer before executing them. JSON success evidence
contains source SHA, installer hash and explicit non-certification flags; it is
written only after uninstall succeeds. Actual PowerShell/Windows/signing execution
remains NOT TESTED in this session. See
`../../engineering/PAPER_NATIVE_HARDENING_2026-11-06.md`.
