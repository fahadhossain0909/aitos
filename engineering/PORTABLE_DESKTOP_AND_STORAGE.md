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
