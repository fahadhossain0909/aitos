# AITOS user-controlled paper signer runbook

## Security boundary

Run the signer on a device or host whose operating system, administrator account, disk and credential vault are controlled by the AITOS owner. **Do not install the signer or place exchange credentials on a provider-managed VPS/RDP that hosts the AITOS runtime.**

The remote runtime receives only a bounded intent API. The signer owns the Binance Futures Testnet API key and secret in the native OS credential vault, authenticates the runtime with TLS 1.3 mutual TLS, independently fetches account/position evidence, applies local risk limits, and sends LIMIT orders only to the fixed Futures Testnet endpoint.

No production Binance endpoint, withdrawal API, live order mode, or automatic retry is included.

## Linux installation preparation

From a trusted Linux host with Python 3, OpenSSL, systemd user services and an unlocked Secret Service/keyring session:

    bash scripts/install_user_controlled_signer_linux.sh

The installer creates a private virtual environment, installs the keyring package, prepares a mode-0700 data directory and writes a user systemd unit. It does **not** start the service, create firewall rules, or install exchange credentials.

Review ~/.config/aitos-signer/signer.env. Keep AITOS_SIGNER_BIND_HOST=127.0.0.1 unless a private VPN/interface path has been configured and firewall-reviewed. Do not expose the listener to the public internet. If wildcard binding is unavoidable, the CLI requires the explicit AITOS_SIGNER_ALLOW_WILDCARD_BIND=1 setting; this is not a substitute for a firewall or network review.

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
