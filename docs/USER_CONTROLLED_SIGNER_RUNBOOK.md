# AITOS user-controlled paper signer runbook

## Security boundary

Run the signer on a device or host whose operating system, administrator account, disk and credential vault are controlled by the AITOS owner. **Do not install the signer or place exchange credentials on a provider-managed VPS/RDP that hosts the AITOS runtime.**

The remote runtime receives only a bounded intent API. The signer owns the Binance Futures Testnet API key and secret through either the native OS credential vault or an explicitly selected external password-manager adapter, authenticates the runtime with TLS 1.3 mutual TLS, independently fetches account/position evidence, applies local risk limits, and sends LIMIT orders only to the fixed Futures Testnet endpoint.

No production Binance endpoint, withdrawal API, live order mode, or automatic retry is included.

## Linux installation preparation

From a trusted Linux host with Python 3, OpenSSL, systemd user services and an unlocked Secret Service/keyring session:

    bash scripts/install_user_controlled_signer_linux.sh

The installer creates a private virtual environment, installs the keyring package, prepares a mode-0700 data directory and writes a user systemd unit. It does **not** start the service, create firewall rules, or install exchange credentials.

Review ~/.config/aitos-signer/signer.env. Keep AITOS_SIGNER_BIND_HOST=127.0.0.1 unless a private VPN/interface path has been configured and firewall-reviewed. Do not expose the listener to the public internet. If wildcard binding is unavoidable, the CLI requires the explicit AITOS_SIGNER_ALLOW_WILDCARD_BIND=1 setting; this is not a substitute for a firewall or network review.

## Desktop signer settings

The Tauri desktop application opens a separate **AITOS — Trusted Signer Settings**
window alongside the read-only Operator Console. The native application menu
contains **Open Trusted Signer Settings**. Closing the signer window hides it
without stopping the local gateway; use the native menu to reopen it. Closing
the Operator Console exits the desktop application and stops the gateway.
The UI requires an explicit trusted-host acknowledgement before enabling
credential operations. This is an operator safety confirmation, not technical
proof of host ownership. Use this local signer window only on the trusted
device that will own the exchange credential pair.

1. Enter the exact account alias configured as
   `AITOS_SIGNER_ACCOUNT_ALIAS` for the signer service (for example,
   `binance-testnet-primary`) and load its current policy. The desktop and
   signer service must run as the same OS user and share the same
   `AITOS_SIGNER_DATA_DIR` (or both use the default data directory).
2. Choose **Global default** or **Per-account override**, then select
   **Native OS vault** or **External password manager**.
3. For External, choose Bitwarden or 1Password and enter the reference for a
   dedicated, operator-created item. A global External reference is shared by
   all accounts without an explicit override; use per-account overrides and
   dedicated items when different exchange accounts use different API keys.
   A global Native policy still stores separate OS-vault slots per account.
4. Save the custody policy for the selected scope.
5. Enter the Binance Futures Testnet API key and secret, confirm the secret,
   then choose **Store and verify credential pair**. The UI clears the fields
   after the operation and reports only whether read-back succeeded.
6. Re-load account status to verify the selected mode and credential-pair
   availability. This is a local vault read check, not an exchange connectivity
   test or permission to submit orders.

The operator console is a different WebView loaded from the loopback gateway.
It receives no signer IPC capability. Native credential commands are accepted
only from the bundled local `signer.html` window. Do not expose the signer UI
over a remote desktop, remote WebView, browser tab, or reverse proxy.

For Bitwarden/1Password, authenticate/unlock the provider CLI and make
its session variables available to the **same parent process environment that
launches AITOS Desktop** (for example, start the desktop executable from the
shell in which the CLI session was established). Unlocking a separate shell
does not automatically update an already-running desktop process. The signer
sidecar inherits the desktop process environment, then passes only the
provider-specific session variables to the selected provider child process.
AITOS does not persist that token. A locked, expired or unavailable provider
fails closed; it never falls back to the native vault automatically.

## Credential custody selection

Choose the credential mode on the **trusted signer host**, before storing the Testnet key:

### Native OS vault

This is the default for a trusted personal Windows, Linux or macOS device:

    PYTHONPATH=/path/to/aitos python -m aitos.security.signer_cli set-credential-mode --mode native

The native backend uses the operating system credential vault. The signer host's OS account and administrator boundary must be trusted.

### External password manager

First create a dedicated, empty item in the password manager. Do not reuse an item containing unrelated secrets.

- **Bitwarden CLI:** install and authenticate the official `bw` CLI on the trusted signer host, unlock the vault for the signer user's session, and use the item's UUID as the reference.
- **1Password CLI:** install and authenticate the official `op` CLI on the trusted signer host, and use `<vault-id>/<item-id>` as the reference.
- The adapters use the concealed custom fields `aitos_exchange_key_id` and `aitos_exchange_api_secret`. AITOS does not persist the provider session token in its profile database.
- Keep provider CLI session material in the signer user's protected session environment only. Do not put it in the repository, runtime VPS, desktop draft, or signer.env.

Configure a global external default (Bitwarden example):

    PYTHONPATH=/path/to/aitos python -m aitos.security.signer_cli set-credential-mode --mode external --provider-id bitwarden --secret-ref <bitwarden-item-uuid>

For 1Password, use `--provider-id onepassword --secret-ref <vault-id>/<item-id>`. Add `--account-alias <alias>` to apply the policy only to one account; without it, the command changes the global default.

Then enter the Testnet key and secret interactively:

    PYTHONPATH=/path/to/aitos python -m aitos.security.signer_cli store-testnet-credential

The provider item must be dedicated to AITOS exchange credentials. The CLI adapters read/update only the two named concealed fields and fail closed on duplicate fields, malformed references, locked sessions, timeouts or provider errors. Switching modes changes routing metadata only; it does not copy or delete credentials in the previous vault. For rotation, stop the signer, provision a new dedicated item, update the reference, store the new credential pair, verify the read-only preflight, and only then retire the old item.

**Critical limitation:** an external password manager does not make a provider-controlled RDP/VPS trustworthy. If the signer process or Bitwarden/1Password CLI session runs on that RDP/VPS, its administrator may inspect process memory, session tokens or decrypted credentials. The signer must still run on a separate user-controlled trusted device (or customer-controlled HSM/KMS service). The RDP/VPS should run only the credential-free AITOS runtime and mTLS intent client.

## Local certificate authority and mTLS certificates

On the signer host, create the local CA, server certificate and runtime client certificate:

    bash scripts/bootstrap_signer_pki.sh DNS:signer.example.internal spiffe://aitos/runtime/paper

Use the DNS name or IP address that the runtime will use to connect; it must match the server certificate SAN. The script stores the CA private key on the signer host with mode 0600 and creates a new client certificate serial for each run.

- Keep client-ca.key on the trusted signer host. Never copy it to the runtime host.
- Configure server.crt, server.key, and client-ca.crt in signer.env.
- Copy only the generated runtime client certificate and its private key to the AITOS runtime host using a secure channel. The client key is an mTLS identity key, **not** an exchange signing key.
- Configure the runtime SignerRPCClient with build_client_context(certificate_chain=..., private_key=..., trusted_server_ca=...).
- Enroll the client certificate on the signer host using the exact command printed by the bootstrap script. The persistent SQLite registry binds the URI SAN to an account and symbol allowlist and enforces the individual certificate serial's validity window.

For rotation, issue a new certificate with the same URI SAN, enroll the new serial, install and verify the new runtime certificate, then revoke the old serial. Use revoke-client to revoke a serial or disable-client to disable the whole identity. Re-enabling a disabled identity requires the explicit enable-client command; enrolling a new serial never silently re-enables it.

## Exchange credential setup

Create a **Binance USD-M Futures Testnet** API key with the minimum permissions required for Testnet account reads and order placement. On the signer host, run:

    set -a
    source ~/.config/aitos-signer/signer.env
    set +a
    PYTHONPATH=/path/to/aitos /path/to/aitos-signer-venv/bin/python -m aitos.security.signer_cli store-testnet-credential

The key ID is entered interactively and the secret is entered through a hidden prompt, then stored via the platform credential vault. Do not put either value in signer.env, shell arguments, repository files, CI variables or the runtime VPS.

The signer performs a read-only Testnet account preflight before opening its RPC listener. Startup fails closed if the credential vault, account endpoint, account permissions, account evidence or TLS material is unavailable.

## Risk and recovery controls

The environment file requires explicit values for:

- allowed account alias and symbols;
- maximum order notional;
- maximum gross and per-symbol exposure;
- maximum daily loss;
- durable per-account order-attempt window and RPC rate window.

The adapter fetches Testnet balances, one-way positions, open LIMIT orders and daily realized PnL/commission/funding receipts. HEDGE mode, malformed evidence, unsupported open-order types, incomplete income history, stale evidence, and mismatched account aliases fail closed.

The SQLite risk store persists order-attempt counts and idempotent realized-net-PnL events. The replay ledger persists intent ID, symbol, client order ID and dispatch state. A recovery worker queries Testnet by the original client order ID after restart and never resubmits. New intents are blocked while any unresolved dispatch remains. An exchange lookup that cannot prove an outcome remains unknown and requires operator reconciliation.

## Start and inspect

After reviewing the environment, placing TLS material, storing the Testnet credential, enrolling the runtime client certificate, and configuring network access:

    systemctl --user enable --now aitos-signer.service
    journalctl --user -u aitos-signer.service -f

The Linux systemd unit runs under the logged-in local user so its native Secret Service session is available. For a headless host, verify that the user service manager and keyring remain available after reboot before enabling unattended startup.

## Acceptance still required outside this repository

The code and CI cannot prove that a real host is controlled by the user. Before relying on the signer:

1. Run the installation on the actual user-controlled host.
2. Verify the signer process, CA private key, exchange credential and SQLite state are absent from the provider-managed runtime host.
3. Verify the runtime can connect only through the intended private network path and that an untrusted/missing/revoked client certificate is rejected.
4. Reboot the signer host and verify the user keyring, certificate registry, rate state, replay ledger and recovery worker restore correctly.
5. Have an independent security reviewer inspect the complete signer, certificate, credential, network and recovery design.

The Testnet adapter is not a production exchange integration. This runbook is not evidence that the user's host has been installed, that production account reconciliation is complete, or that an independent security audit has passed.
