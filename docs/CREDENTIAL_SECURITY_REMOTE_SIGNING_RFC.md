# AITOS Credential Security & Remote Signing RFC

- **Status:** Proposed architecture; policy-validation reference implementation is included, but no signer, exchange dispatch, or live capability is enabled.
- **Date:** 2026-11-10
- **Scope:** Desktop credential handling, provider-hosted RDP/VPS threat boundary, remote order-intent authorization, replay protection, failure behavior, rollout and verification.
- **Related:** `aitos/native/credential_vault.py`, `aitos/native/windows_vault.py`, `aitos/security/secrets.py`, `docs/OPERATOR_GATEWAY.md`, `INSTALLATION.md`.

## 1. Executive decision

AITOS must not place an exchange API secret on a provider-controlled RDP/VPS when the security requirement is that the provider's host administrator must not be able to recover or use it.

Encryption at rest is necessary but insufficient. A process that decrypts a secret to create an HMAC signature exposes the secret to the host on which that process runs. A provider administrator with sufficient control can inspect process memory, instrument or replace binaries, capture input, alter libraries, read files and environment variables, or invoke the process as the logged-in user. TPM-backed storage and OS credential vaults improve protection against offline disk theft and other local users; they do not make a compromised host administrator untrusted-proof.

**Decision: separate strategy execution from credential custody and exchange signing.**

- The VPS/RDP runs the AITOS market-data, research, strategy, risk-proposal and paper-trading components. It has no exchange secret and no general signing capability.
- A signer runs on a separately trusted, user-controlled device or in a customer-controlled signing/HSM service. It owns the exchange credential and makes the final authorization decision.
- The VPS sends a bounded, short-lived **order intent**, never an API key, secret, raw signature, or arbitrary exchange request.
- The signer validates identity, policy, freshness, replay state, account/position evidence and risk limits, then signs and dispatches the exchange request itself. It returns a redacted execution receipt, not a reusable signature or secret.
- Live dispatch remains disabled until the separate implementation, security, exchange-adapter and operational gates in this RFC are met and explicitly approved.

This architecture reduces credential exposure on the provider host. It does not claim protection if the user's signer device, operating-system account, exchange account, or signer process is compromised.

## 2. Security goals and non-goals

### 2.1 Goals

1. No raw exchange API secret or private signing key reaches a provider-controlled AITOS runtime.
2. No raw signature is returned to the VPS for later or arbitrary replay.
3. Only authenticated, authorized, fresh and non-replayed order intents can reach the exchange adapter.
4. The signer enforces independent policy limits even if the VPS strategy, risk engine, operator UI or network is compromised.
5. Unavailable or ambiguous authorization, replay storage, position evidence, risk state, or exchange response fails closed for exposure-increasing orders.
6. Every decision is attributable using correlation IDs and redacted audit evidence without recording credentials, signatures, cookies or full signed URLs.
7. Paper mode remains the default. No new credential path silently enables live execution.

### 2.2 Non-goals / honest limitations

- No software running on a provider-controlled host can hide data from a sufficiently privileged administrator of that same host.
- AES-GCM, DPAPI, Credential Manager, Secret Service, Keychain, TPM and Secure Enclave are not substitutes for moving the signer outside the hostile host.
- Python cannot guarantee zeroization of all secret copies, prevent memory inspection by a privileged local administrator, or provide a formally verified signing boundary.
- mTLS authenticates a client identity; it does not prove that a compromised client is issuing a safe order.
- A valid HMAC proves possession of a secret, not that an order is economically safe or authorized by the user.
- Remote signing is not equivalent to a hardware security module. Customer-controlled HSM/KMS can improve key custody, but authorization policy, host compromise, availability and operational governance still matter.
- This RFC does not certify Binance, other exchanges, or any production deployment.

## 3. Threat model

### 3.1 Assets

- Exchange API key identifier and API secret / private signing key.
- Exchange account balances, positions, orders and trading permissions.
- Signer identity keys, client certificates and policy configuration.
- Order intent integrity, freshness, idempotency and authorization.
- Risk limits, daily loss state, exposure state and audit evidence.
- Operator and recovery credentials.

### 3.2 Adversaries

| Adversary | Assumed capability | Required defense |
|---|---|---|
| VPS/RDP provider administrator | Root/admin, disk and process inspection, service replacement, network observation | No exchange secret or reusable signature on provider host |
| Compromised AITOS process | Arbitrary order-intent generation and retries | Signer-side allowlists, limits, expiry, replay protection, independent evidence |
| Network attacker | Observe, delay, replay, reorder or modify traffic | TLS/mTLS, message authentication, nonce, expiry, idempotency |
| Stolen operator password | Attempt console access | Operator credential separate from exchange credentials; bounded read-only gateway |
| Local unprivileged process | Attempt vault/API access | OS credential store, least privilege, per-user isolation |
| Compromised signer device/account | Read signer memory or invoke signer | Out of scope for software-only custody; isolate device, OS, account and hardware boundary |

### 3.3 Trust zones

**Zone A — User-controlled signer boundary (trusted only to the extent the device is trusted):**
- Secure Signing Agent (ASSA).
- OS credential vault / optional hardware-backed key protection.
- Signer policy and durable replay/idempotency store.
- Exchange adapter and signed-request dispatch.
- Local redacted audit journal and operator approval channel.

**Zone B — Provider-controlled compute (untrusted for credential custody):**
- AITOS market-data ingestion, feature computation, strategy, research and candidate selection.
- Risk proposal generation and paper trading.
- Order-intent client.
- No exchange secret, no signer private key, no generic signing endpoint, no ability to request arbitrary bytes be signed.

**Zone C — Exchange:**
- API key with least-privilege trading permissions.
- Withdrawals disabled.
- IP restrictions where supported and operationally compatible.
- Separate key/subaccount for testing and production.

The provider host is treated as hostile even when its network transport is encrypted.

## 4. Architecture and data flow

```text
       USER-CONTROLLED DEVICE / TRUSTED SIGNER ZONE
  +----------------------------------------------------+
  | AITOS Secure Signing Agent (ASSA)                  |
  |  +----------------+  +---------------------------+ |
  |  | OS Vault /     |  | Independent Policy Engine | |
  |  | HSM / Key API  |  | symbol / size / exposure  | |
  |  +-------+--------+  | expiry / loss / reduce-only| |
  |          |           +-------------+-------------+ |
  |          |                         |               |
  |  +-------v-------------------------v------------+  |
  |  | Durable nonce + idempotency + audit journal  |  |
  |  +----------------------+-----------------------+  |
  |                         |                          |
  |                  Exchange Adapter                  |
  +-------------------------+--------------------------+
                            | signed HTTPS request
                            v
                         EXCHANGE

             TLS + client identity + bounded intent
                            ^
                            |
  +-------------------------+--------------------------+
  | PROVIDER-CONTROLLED VPS / RDP (UNTRUSTED)          |
  | Market data -> Strategy -> Risk proposal           |
  | Paper engine -> Order Intent Client                |
  | No exchange secret / no signing private key        |
  +----------------------------------------------------+
```

### 4.1 Preferred dispatch model

The signer dispatches the signed request directly to the exchange. Do **not** return a raw HMAC signature or signed URL to the VPS. Returning a signature lets a compromised VPS race, replay or alter request context, and may expose signed query material in logs or telemetry.

The VPS receives only a minimal receipt:
- intent ID and client order ID;
- accepted/rejected/unknown outcome;
- exchange order ID when confirmed;
- redacted reason code;
- server-side timestamp and correlation ID.

Never return API credentials, signature bytes, signed query strings, authorization headers or raw exchange response bodies.

### 4.2 Connectivity modes

1. **Local signer mode:** signer runs on the user's own desktop. The desktop must remain online and connected for signing. A powered-off or sleeping device causes fail-closed order-intent rejection/queue expiry.
2. **Dedicated user-controlled signer host:** a separate device/server administered by the user, isolated from the VPS provider account. Apply OS hardening, patching, firewalling, key vault, backup and monitoring.
3. **Customer-controlled HSM/KMS:** signer uses a key operation service under the user's control. Confirm the provider supports the exchange's exact signing primitive and key format. HMAC API secrets are not automatically portable to asymmetric HSM keys.
4. **Same-provider RDP/VPS signer:** **not an acceptable mode** when the requirement is to hide credentials from that provider's administrator.

## 5. Order-intent contract

The remote runtime submits a constrained JSON intent over TLS with a client identity. The signer does not accept arbitrary HTTP paths, exchange parameters, headers, signatures or raw payloads.

### 5.1 Request schema (illustrative v1)

```json
{
  "schema_version": 1,
  "intent_id": "opaque-unique-id",
  "nonce": "at-least-128-bits-of-randomness",
  "issued_at_ms": 1794310000000,
  "expires_at_ms": 1794310030000,
  "account_alias": "paper-or-approved-account-alias",
  "symbol": "BTCUSDT",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": "0.001",
  "limit_price": "65000.00",
  "reduce_only": false,
  "client_order_id": "aitos-unique-client-order-id",
  "strategy_id": "strategy-version-id",
  "candidate_id": "candidate-id",
  "decision_id": "decision-ledger-id",
  "reason_code": "approved-strategy-reason"
}
```

The sample is a schema example, not an order recommendation. It must never be copied into a live account as-is.

### 5.2 Contract rules

- Reject unknown fields, duplicate JSON keys, non-finite values, floats, exponent-form decimal strings, negative/zero quantities, invalid precision, oversized bodies and unsupported schema versions.
- Decimal quantities/prices are strings; no binary floating-point values cross the authorization boundary.
- `intent_id`, `nonce` and `client_order_id` are unique and bounded. The nonce must contain at least 128 bits of cryptographic randomness.
- Maximum intent lifetime is short (initial target: 30 seconds); reject future-issued, expired or clock-skewed requests.
- Bind the authenticated client identity to an explicit AITOS runtime/environment/account alias. A client certificate alone does not authorize symbols or order size.
- Signer derives the exchange account and API key from its own local configuration; the caller cannot choose arbitrary credentials or account IDs.
- Only allowlisted symbols, sides and order types are accepted.
- Market orders are disabled by default. If enabled, the signer obtains a fresh trusted reference price and applies a conservative slippage/notional bound.
- Limit orders must have a valid price and exchange tick/lot precision. Market orders must not carry a caller-controlled price as a valuation authority.
- A reduce-only claim is not trusted by itself. The signer must reconcile a fresh position snapshot and prove that the order reduces exposure without crossing through zero. If position evidence is stale or ambiguous, reject.
- A strategy label, candidate ID or decision ID is provenance metadata, not authorization.
- No cancel-all, withdrawal, transfer, account configuration, leverage change, margin-mode change, API-key management or arbitrary endpoint capability is included in v1.

The initial policy validator is implemented in `aitos/security/signing_policy.py`. It validates a bounded intent and configured per-order policy; it does not authenticate transport identity, persist replay state, reconcile positions, sign or dispatch orders. Those remain explicit integration gates.

## 6. Signer authorization policy

Policy is configured and approved on the signer boundary, never dynamically supplied by the VPS.

### 6.1 Minimum policy dimensions

- Explicit environment and account alias.
- Symbol allowlist.
- Allowed sides and order types.
- Per-order maximum notional.
- Per-symbol maximum position notional and portfolio gross/net exposure.
- Daily realized + unrealized loss / drawdown stop.
- Maximum order count and request rate.
- Maximum open orders and outstanding intents.
- Maximum price deviation / slippage.
- Reduce-only semantics with trusted position reconciliation.
- Trading schedule and emergency disable switch.
- Separate paper/test and live policies; live policy is absent/disabled by default.

### 6.2 Fail-closed requirements

Reject exposure-increasing requests if:
- policy is missing, invalid, stale or unsigned;
- caller identity or environment binding is unknown;
- nonce/idempotency store is unavailable;
- account, balances, open orders or positions cannot be reconciled;
- market price or exchange metadata is stale;
- risk ledger is unavailable or inconsistent;
- intent expired or clock drift exceeds tolerance;
- the exchange outcome of a prior request is unknown and cannot be reconciled;
- daily loss, notional, rate or position limits are reached.

Do not convert a failed risk check into a warning or bypass it with a strategy override.

### 6.3 Reduce-only and close handling

A reduce-only flag is not sufficient proof. For a long position, a sell quantity must not exceed the reconciled long quantity; for a short position, a buy quantity must not exceed the reconciled short quantity. Account mode, hedge mode, position side and exchange-specific semantics must be explicitly modelled. Unknown position state means no close dispatch until reconciled; do not risk flipping a position by guessing.

## 7. Authentication, integrity and replay protection

- Use TLS 1.3 where available. Prefer mutual TLS with a dedicated client certificate per runtime/environment. Pin/rotate trust roots through an authenticated administrative process.
- Bind certificate identity to the runtime ID, environment, allowed account alias and policy version.
- The signer validates the canonical request structure and authenticated transport identity before policy evaluation. A client certificate stored on the VPS can itself be copied by a provider administrator; mTLS identifies the runtime credential, but does not make that identity resistant to a hostile host administrator. The signer must assume that an attacker may fully impersonate the VPS client and rely on independent signer-side policy and limits.
- Use a durable atomic uniqueness constraint for `(client_identity, nonce)`, `intent_id` and `client_order_id`. In-memory-only replay protection is not production-safe across restarts or multiple signer replicas.
- Store the idempotency state before dispatch. States include `RECEIVED`, `AUTHORIZED`, `DISPATCHING`, `CONFIRMED`, `REJECTED`, `UNKNOWN`, and `RECONCILED`.
- If the signer crashes after sending but before receiving a response, mark the outcome `UNKNOWN`; query/reconcile by client order ID before any retry. Never blindly resend an exposure-increasing request.
- Exchange client order IDs must be deterministic per accepted intent and unique within exchange constraints.
- Use an authenticated message digest or signed envelope if intents are queued through intermediaries. Canonicalization must be specified and tested; do not sign a JSON string whose whitespace/key order can change.
- Keep clocks synchronized. The signer uses its own clock for expiry checks; remote timestamps are not trusted as authoritative.

## 8. Credential vault and key custody

### 8.1 Existing AITOS state

AITOS already has an OS-backed credential-store abstraction:
- Windows Credential Manager via `aitos/native/windows_vault.py`.
- Linux Secret Service / keyring via `aitos/native/credential_vault.py`.
- macOS Keychain adapter path in the keyring abstraction.

These are preferable to plaintext files for local desktop use. The existing code explicitly fails closed when the required backend is unavailable. However, these stores are scoped to the local OS user and are **not protection from that machine's compromised administrator/root or malware running as that user**.

The current desktop installation guide describes an operator password and backend bearer token. The read-only desktop gateway has no exchange credential fields or trading routes. Do not repurpose the operator password, backend bearer or `AITOS_CONSOLE_TOKEN` as an exchange API credential.

### 8.2 Storage design for the signer

- Store exchange credentials only on the trusted signer boundary.
- Prefer OS vault APIs; use TPM/Secure Enclave or customer-controlled HSM/KMS where compatible.
- If application-level envelope encryption is needed for portable encrypted backup, use a vetted AEAD such as AES-256-GCM or XChaCha20-Poly1305 with unique nonces, authenticated metadata, key versioning and a KEK outside the encrypted database.
- Derive passphrase keys with a memory-hard KDF such as Argon2id only when a passphrase-based unlock is actually required; enforce parameters and rate limits. Do not invent custom cryptography.
- Never store plaintext secrets in SQLite/Postgres/ClickHouse, JSON/YAML, `.env`, shell history, CI logs, telemetry, crash dumps, browser storage or source control.
- Avoid passing secrets in command-line arguments. Avoid exposing them in environment variables where a protected OS secret API is available.
- Minimize secret lifetime and copies; never log signed URLs, headers, HMAC values or raw request objects. Python string zeroization cannot be guaranteed.
- Credential import must be local, interactive, hidden from remote console/session capture where possible, and immediately stored in the trusted vault. The provider-hosted desktop must not offer exchange-key entry for a protected remote-signing configuration.

### 8.3 GitHub Actions and VPS secrets

GitHub Actions environment secrets can authenticate deployment and protect CI configuration, but any secret written into a VPS runtime environment becomes available to sufficiently privileged host administrators and processes. Therefore:
- Deployment SSH keys and deployment tokens are separate from exchange credentials.
- Do not add exchange API secrets to the GitHub `papertrade` environment for the purpose of hiding them from the VPS provider.
- The existing `AITOS_CONSOLE_TOKEN` is a backend console bearer, not an exchange key. Protect it, rotate it, and keep the gateway read-only.
- Remove legacy exchange secrets from VPS env files and CI once a reviewed migration plan exists; do not delete/rotate unknown production credentials automatically.

## 9. Exchange account controls

Before any live integration:
- Disable withdrawals and transfers on the API key.
- Grant only the minimum trading/read permissions required.
- Use a dedicated subaccount and API key for AITOS.
- Enable IP allowlisting where compatible with the signer egress path.
- Keep paper/test credentials separate from production credentials.
- Define key owner, creation date, scope, rotation interval, revocation owner and incident procedure.
- Test revocation and replacement using a non-production key first.
- Never request or store exchange passwords, 2FA seeds or withdrawal permissions.

## 10. API surface (proposed)

The signer API is intentionally small:

| Endpoint | Method | Purpose | Default |
|---|---|---|---|
| `/v1/health` | GET | Liveness only; no secret/account data | Enabled on private interface |
| `/v1/capabilities` | GET | Non-sensitive policy capability summary | Disabled until authenticated |
| `/v1/intents` | POST | Submit one bounded order intent | Disabled / paper-only |
| `/v1/intents/{id}` | GET | Read redacted intent state/receipt | Authenticated |
| `/v1/disable` | POST | Emergency disable; signer-local authorization only | Local control only |

There is no endpoint to:
- retrieve credentials or signatures;
- sign arbitrary bytes, URLs, query strings or exchange requests;
- proxy arbitrary exchange endpoints;
- change policy from the remote runtime;
- enable live mode;
- change leverage, margin, withdrawal or transfer settings.

The `/v1/disable` route must not be remotely callable by the untrusted runtime. Emergency disable is controlled locally or through a separately authenticated owner channel.

## 11. Failure and recovery matrix

| Condition | Required behavior |
|---|---|
| Signer offline / device asleep | Do not sign; expire intents; paper runtime may continue |
| mTLS identity invalid | Reject and audit coarse reason |
| Expired or replayed nonce | Reject; no exchange request |
| Policy missing or invalid | Reject all live intents |
| Position/balance snapshot stale | Reject exposure increase and ambiguous reduce-only |
| Risk ledger unavailable | Reject exposure increase |
| Exchange timeout before response | Mark UNKNOWN; reconcile before retry |
| Exchange accepted but receipt lost | Query by client order ID; do not resubmit blindly |
| Signer restart | Recover durable idempotency and audit state before accepting intents |
| Vault locked/unavailable | No credential fallback; signer remains disabled |
| Clock drift | Reject until trusted clock restored |
| Certificate/key rotation incomplete | Reject unknown identities; retain explicit rollback plan |
| Emergency stop | Stop new dispatch; reconcile in-flight requests; do not erase order/position ledger |

The signer must not queue stale intents for later execution. Queues preserve audit evidence, not authorization; an intent must be revalidated against current policy and current market/account state at dispatch time.

## 12. Audit, privacy and observability

Record:
- intent ID, runtime identity, account alias, policy version;
- accepted/rejected reason code;
- symbol, side, order type, quantity and limit price where permitted;
- reference-price timestamp and risk-check evidence identifiers;
- exchange client order ID and confirmed exchange order ID;
- dispatch/reconciliation timestamps and state transitions.

Never record:
- API key secret, private key, HMAC signature, signed query string;
- Authorization headers, client private keys, session cookies or operator password;
- raw environment dumps, full request headers or unredacted exchange error payloads.

Audit storage should be append-only or tamper-evident, access-controlled, time-synchronized and independently backed up. Audit availability is a gate for live dispatch; if mandatory audit persistence fails, reject.

## 13. Implementation plan and acceptance gates

### Phase 0 — Security boundary and documentation (this change)

- Document threat model, zones, data flow, intent contract, policy, replay, failure and recovery.
- Explicitly distinguish the existing read-only operator gateway from a signer.
- Add a pure, fail-closed order-intent policy validator with synthetic unit tests.
- No network listener, credential import, signing, exchange dispatch or live enablement.

**Acceptance:** unit tests prove malformed, expired, oversized, unknown-field, disallowed-symbol/order-type and notional-over-limit intents are rejected. The module cannot access credentials or send network requests.

### Phase 1 — Local signer skeleton (separate reviewed change)

- Add a dedicated `aitos.signer` process with loopback-only bind by default.
- Add signer-local configuration and explicit disabled-by-default live switch.
- Integrate OS vault through the existing `CredentialStore` protocol.
- Add durable SQLite/Postgres replay/idempotency store with atomic uniqueness.
- Add mTLS client identity and certificate rotation; no remote policy mutation.
- Implement paper-only fake exchange adapter first.

**Acceptance:** process isolation, locked vault, restart recovery, replay race, certificate rejection and redaction tests pass.

### Phase 2 — Independent risk and account evidence

- Reconcile account, positions, open orders, balances and exchange metadata directly from the exchange.
- Implement symbol/side/type/notional/position/gross/net/daily-loss/rate/slippage policy.
- Verify reduce-only behavior for one-way and hedge modes.
- Fail closed on stale evidence, unknown mode, partial fills, duplicate requests and ambiguous exchange outcomes.

**Acceptance:** property-based and fault-injection tests; no increase beyond configured limits under concurrent requests; independent account reconciliation.

### Phase 3 — Paper exchange adapter and end-to-end replay

- Connect signer to a sandbox/testnet or deterministic local exchange simulator.
- Exercise network interruption at every dispatch boundary, signer restart, duplicate messages, timeout, partial fill and cancel/reconcile.
- Compare intent, signer ledger and exchange order state.
- Ensure provider VPS never receives API secret or signed request material.

**Acceptance:** repeatable end-to-end tests and security review. No live key required.

### Phase 4 — Restricted live pilot (explicit separate authorization)

- Use a dedicated subaccount and least-privilege API key with withdrawals disabled.
- Start with minimal notional, small symbol allowlist, manual local signer approval and short observation window.
- Keep automatic scaling, leverage changes, withdrawals and policy changes disabled.
- Independent security review, rollback and incident runbook required.

**Acceptance:** owner approval, independent code review, successful reconciliation/soak, monitoring, tested revocation and documented residual risks. Passing CI alone is insufficient.

## 14. Test matrix

| Test | Expected result |
|---|---|
| Raw API secret accidentally appears in intent | Schema rejects unknown field; no log leakage |
| Duplicate JSON key / unknown field | Reject before policy |
| Float, exponent, NaN, Infinity, negative quantity | Reject |
| Expired / future intent | Reject |
| Nonce replay / concurrent duplicate | Exactly one durable acceptance |
| Unknown client certificate | Reject |
| Valid certificate but wrong runtime/account binding | Reject |
| Disallowed symbol / side / type | Reject |
| Market order while disabled | Reject |
| Notional above configured cap | Reject |
| Price / quantity precision invalid | Reject |
| Stale account or position snapshot | Reject exposure increase |
| Reduce-only quantity exceeds reconciled position | Reject |
| Risk ledger missing / daily loss limit reached | Reject |
| Signer unavailable | No signature/dispatch; intent expires |
| Crash before dispatch | Recover state without duplicate |
| Crash after exchange accepted but before receipt | UNKNOWN then reconcile by client order ID |
| Exchange timeout / partial fill | Reconcile; no blind retry |
| Vault locked / unavailable | No fallback; signer disabled |
| Audit store unavailable | Reject live dispatch |
| Secret in logs, metrics, traces, crash report | Test fails |
| VPS filesystem/process inspection | No exchange secret or reusable signature present |
| Desktop app on provider-controlled RDP | No exchange credential-entry path |

## 15. Rollout, migration and rollback

- Keep the existing read-only desktop gateway and backend bearer contract unchanged.
- Keep all current paper-trading workflows and deployment behavior unchanged.
- Do not silently migrate existing environment secrets. Inventory their use, owner and scope first.
- Do not delete, rotate or revoke any existing credential without explicit owner approval and a verified replacement/recovery plan.
- Rollback of the RFC/policy module is a code revert; it does not require touching a live runtime.
- If a signer is compromised, locally disable dispatch, revoke the exchange key at the exchange, reconcile outstanding orders/positions, preserve audit evidence, then rotate credentials and certificates.
- If the signer loses connectivity, paper execution may continue; live intents must not be buffered for later unreviewed execution.

## 16. Current implementation status

| Capability | Status |
|---|---|
| Windows Credential Manager adapter | Existing local operator/backend-token storage |
| Linux Secret Service adapter | Existing local operator/backend-token storage |
| macOS Keychain via keyring adapter | Existing abstraction; platform-specific validation still required |
| Read-only operator gateway | Implemented; no exchange credential fields or trading routes |
| Threat model and remote-signing RFC | Added by this change |
| Strict order-intent policy validator | Added as a non-networked reference component |
| Durable replay/idempotency | Not implemented |
| mTLS identity binding | Not implemented |
| Trusted position/account reconciliation | Not implemented |
| Signer process / vault credential binding | Not implemented |
| Exchange signing / dispatch | Not implemented |
| Live mode | Not implemented or authorized |

**Bottom line:** AITOS can safely run its strategy and paper runtime on a provider VPS without giving that VPS the exchange secret only after the signing boundary is moved to a separately trusted signer. Installing the desktop app on the provider's own RDP and entering the exchange secret there does not meet the stated threat model.


## 12. Incremental reference implementation status (2026-11-10)

The branch now includes a deliberately limited local/paper reference implementation:

- `aitos/security/signing_policy.py`: bounded JSON parser and pure, fail-closed intent policy validation with strict integer/boolean types and sufficient Decimal precision for the accepted input bounds.
- `aitos/security/replay_store.py`: SQLite ledger with `BEGIN IMMEDIATE`, WAL, `synchronous=FULL`, unique account-scoped nonce and client-order identities, SHA-256 nonce/fingerprint storage, private POSIX parent/database permissions, a configurable hard capacity ceiling, and monotonic state transitions.
- `aitos/security/paper_processor.py`: in-process paper-only intent processor. It has no HTTP listener, credential provider, cryptographic signing, exchange client or network dispatch.
- `tests/security/test_signing_policy.py` and `tests/security/test_replay_store.py`: schema/policy, duplicate-key, durable reopen, collision, concurrent reservation, state-transition, ambiguous-dispatch and no-auto-retry tests.

### State machine

```text
RESERVED -> DISPATCHING -> ACCEPTED
                        -> REJECTED
                        -> UNKNOWN
```

A crash after the durable `DISPATCHING` transition leaves the intent ambiguous. Re-submitting the same intent returns its existing state and does not call the adapter again. The store intentionally has no automatic reset, delete, or retry transition. An operator must reconcile the venue/account evidence through a separately designed workflow before any future action.

### Important implementation limits

- SQLite is a single-host durable ledger, not a multi-host consensus system or distributed lock. Do not put it on a shared network filesystem or run multiple active signer hosts against copied databases.
- The database parent directory must be private on POSIX and the database file is chmod 0600; the ledger fails closed at its configured record ceiling rather than deleting replay history. Production must also secure WAL/SHM files, backups, service account and host. Windows ACL hardening is a separate platform adapter.
- The paper processor accepts LIMIT intents only and uses an adapter marked paper-only. It is not a remotely reachable service.
- No production nonce store, mTLS transport, caller identity authentication, trusted account/position reconciliation, daily-loss/exposure ledger, exchange credential binding, HSM/KMS integration or live exchange dispatch is implemented.
- A durable local reservation is not proof of exactly-once execution at an exchange. Venue-side idempotency and independent reconciliation are still required.

### Acceptance gates before any live signer

1. Independent security review of canonical request encoding, nonce generation, identity authentication and signer policy.
2. Process-kill/power-loss tests at every boundary before and after dispatch, including DB corruption, disk-full, backup restore and ambiguous venue acknowledgement.
3. Cross-process and multi-instance contention tests on the exact supported filesystem and OS.
4. Exchange-specific signed-request conformance tests against a sandbox/testnet with withdrawals disabled and a dedicated low-risk account.
5. Independent account/position/order reconciliation and explicit operator resolution of UNKNOWN intents.
6. Redacted audit retention, key rotation/revocation, recovery drills and monitoring.
7. Separate written approval before enabling any live dispatch. Paper-only behavior remains the default.


## 12. Implemented paper-only foundation (2026-11-11)

The current branch contains a deliberately non-live reference implementation:

- `aitos/security/signing_policy.py`: bounded, strict order-intent parser and per-order allowlist.
- `aitos/security/replay_store.py`: SQLite WAL/FULL durable idempotency ledger. Ambiguous DISPATCHING rows are never automatically resubmitted. Capacity exhaustion fails closed; POSIX parent/database permissions are checked.
- `aitos/security/paper_processor.py`: in-process paper adapter only; it has no HTTP listener, credential access, signature generation or exchange connectivity.
- `aitos/security/risk_limits.py`: independent risk preflight requires fresh, account-matched evidence and enforces gross exposure, symbol exposure, daily-loss and order-rate ceilings. Reduce-only requires a matching position and cannot flip exposure. This is a pure gate; the evidence source is not yet a production exchange reconciler.
- `aitos/security/signer_credentials.py`: account-scoped credential slots backed by the injected OS vault; instantiate only inside the trusted signer boundary. Partial writes are rolled back where possible.\n- `aitos/security/mtls.py`: TLS 1.3 mutual-authentication context builders and exact URI-SAN client identity pinning. This is transport configuration only, not a deployed remote signer service.
- `aitos/native/credential_vault.py` and `aitos/native/windows_vault.py`: existing OS-vault adapters (Linux Secret Service, macOS Keychain backend, Windows Credential Manager). These are local credential-store primitives, not proof against an administrator of the same host.

### 12.1 Required production gaps

1. Implement a separate signer process/service on a user-controlled device, with a narrowly scoped authenticated protocol and certificate enrollment/rotation/revocation.
2. Implement a trusted exchange account adapter that fetches balances, open orders, positions, fills and realized PnL, then reconciles evidence freshness, sequence and completeness. Never accept VPS-supplied position or balance claims as signer evidence.
3. Persist risk state and order-rate windows durably across restart; define UTC trading-day boundaries, fee/funding treatment, equity high-water mark and loss accounting.
4. Integrate the signer with a user-approved OS vault slot and exchange-specific signing adapter. Do not expose signatures or signed URLs to the VPS.
5. Add multi-process contention, forced termination at every state boundary, disk-full, read-only filesystem, WAL recovery, backup/restore and capacity-exhaustion drills.
6. Add certificate provisioning and rotation tests, TLS handshake integration tests, authorization binding from certificate identity to runtime/account, and network timeout/replay tests.
7. Conduct independent security review and explicitly approve any future live capability. Until then the processor and adapters remain paper-only.

**Important:** the risk snapshot type is an evidence contract, not an evidence collector. Passing fabricated or stale account data is not secure. A production signer must populate it from its own authenticated exchange session and fail closed if reconciliation is incomplete.


## 13. Signer-side paper preflight wiring

The paper-only processor now supports a paired `SignerRiskLimits` + `AccountEvidenceProvider` configuration. When enabled, it calls the injected provider from inside the processor using the intent's account alias and signer clock, then validates the returned snapshot **before** reserving the nonce or dispatching to the paper adapter. The order-intent JSON schema has no account-snapshot field; the VPS cannot attach its own balance/position claims to the intent.

The provider contract is intentionally an interface, not a production exchange adapter. A production implementation must execute authenticated account reads from the signer-owned credential session and reconcile balances, open orders, positions, fills, realized PnL, fees and funding. It must reject partial pagination, stale timestamps, inconsistent snapshots, unknown symbol metadata and incomplete exchange responses. Provider errors propagate and prevent reservation/execution. The current risk snapshot's order counter must be backed by a durable signer-owned rate window; a caller-supplied counter is not acceptable.

The risk preflight currently checks account alias, evidence freshness, gross/symbol exposure, daily-loss ceiling, order-rate ceiling and reduce-only position direction/quantity. It is a conservative pre-trade gate, not a complete portfolio margin/liquidation engine. It does not yet calculate multi-asset correlated exposure, available margin, maintenance margin, liquidation distance, pending-order commitments or fee/funding-adjusted daily equity.

## 14. Implementation status and non-claims

- **Implemented in source:** TLS 1.3 mTLS context builders and URI-SAN allowlist matching helper; OS-vault-backed signer credential slots; durable local replay ledger; strict intent schema; paper-only processor; optional signer-side risk preflight with a provider interface; tests for crash/duplicate and provider-unavailable behavior.
- **Not implemented / not production-ready:** network listener or remote signing RPC, certificate issuance/rotation/revocation, binding mTLS identity to account authorization at a running endpoint, exchange-specific signer/dispatch adapter, trusted exchange account-evidence implementation, durable rate-window aggregator, multi-process single-writer service ownership, operator approval UX, HSM support, high-availability failover, backup/restore drills and live reconciliation.
- **Security boundary:** the credential-slot helper must execute only on the user-controlled signer host. It does not encrypt credentials against an administrator of the same host. The provider-hosted VPS must never receive the secret, signer private key, or reusable signed request.
- **Execution status:** no live exchange adapter, real order dispatch, production credential migration, deployment or live activation is included in this change. The processor remains paper-only.

A separately deployed signer is not complete until the missing controls above have been implemented, integration-tested and independently reviewed. Do not treat the current paper implementation or green unit tests as production authorization.


## 15. Bounded signer RPC contract and certificate authorization (2026-11-11)

The branch adds `aitos/security/signer_rpc_protocol.py`, a pure protocol-validation layer. It is deliberately **not** an HTTP/gRPC listener and does not create a socket.

Implemented controls:
- exact URI-SAN identity selection from the TLS-verified peer certificate;
- identity-to-account and symbol allowlist grants;
- certificate serial allowlisting, explicit serial revocation, enabled/disabled grants and validity windows;
- overlapping allowed serials to support controlled certificate rotation, followed by explicit removal of the old serial;
- bounded UTF-8 JSON envelope parsing, duplicate-key rejection, strict protocol version and request ID;
- request timestamp skew checks and account/symbol scope enforcement;
- explicit rejection of API secrets, private keys, signatures and reusable signed-request fields;
- immutable parsed intent mapping.

The protocol hands the intent to the existing strict intent validator and durable replay processor; it does not replace their validation or consume a nonce itself. The authenticated identity parameter is expected to be supplied by the TLS connection handler after certificate verification. A future listener must make that call path unforgeable by deriving identity directly from the accepted TLS socket, never from an HTTP header or request JSON.

Certificate lifecycle limitation: this code supports a static, in-memory authorization snapshot and serial revocation policy. It does not issue certificates, persist registry updates, watch a revocation feed/CRL/OCSP, rotate private keys, or expose an operator enrollment workflow. A process restart reloads policy from trusted deployment configuration, which is not yet implemented here.

No live signing, exchange dispatch, account API adapter, production listener, deployment or credential migration is included.


The request parser is paired with `authenticate_signer_request`, which obtains the identity/grant from the supplied TLS peer certificate and then parses the bounded request. A future socket handler must pass only the peer certificate returned by the accepted TLS socket. Forwarded certificate headers, JSON identity claims and caller-selected account grants are not trusted identity sources. This remains a callable protocol helper, not a running listener.


## 16. Dual-mode credential custody (2026-11-11)

AITOS supports two credential-custody modes behind one signer credential interface.
The selected policy is resolved by account alias; a global default applies only
when that account has no explicit override.

### Native mode

- The signer host stores the exchange key ID and API secret in its pinned OS
  credential store: Windows Credential Manager, Linux Secret Service, or the
  macOS Keychain backend.
- Native mode is intended only for a user-controlled, trusted signer device.
  It does not protect against a compromised OS or an administrator of that
  same host.
- A provider-managed RDP/VPS is not a trusted signer host merely because the
  desktop application is installed there.

### External mode

- The signer resolves credentials through an explicitly installed external
  vault adapter. Adapters are discovered only inside the signer process through
  the `aitos.credential_vaults` Python entry-point group.
- Each adapter must implement `get_secret`, `put_secret`, and
  `delete_secret`; the adapter owns provider authentication and must never
  return access tokens or diagnostics to AITOS logs.
- The local SQLite profile database stores only mode, provider ID, and opaque
  vault reference metadata. It does not store exchange secrets or provider
  authentication tokens.
- No provider-specific Bitwarden, 1Password, or hosted-vault adapter is bundled
  by this tranche. Installing/configuring a concrete adapter and testing its
  authentication, unlock, rotation and failure behavior are separate required
  integration work.

### Routing and failure behavior

- `aitos/native/credential_profiles.py` implements durable global and
  per-account policy, external adapter discovery, and a credential-store router.
- `aitos/security/signer_cli.py set-credential-mode` configures the global
  default or one account override. The mode must be explicit; an absent profile
  fails closed.
- If an External provider is unavailable, locked, or fails authentication,
  AITOS fails closed. It does not silently copy credentials into the native
  vault or use environment variables/files as a fallback.
- The existing `SignerCredentialSlots` API is unchanged; the router is injected
  beneath it, so exchange clients do not need to know which custody mode is in use.
- API credentials remain signer-local. The remote runtime receives only the
  bounded authenticated order-intent protocol, never API secrets, vault tokens,
  signatures or reusable signed requests.

### Operator commands

Set a native global default:

```bash
python -m aitos.security.signer_cli set-credential-mode --mode native
```

Override one account to native mode:

```bash
python -m aitos.security.signer_cli set-credential-mode --mode native --account-alias primary
```

Set an external global default after installing a provider adapter:

```bash
python -m aitos.security.signer_cli set-credential-mode --mode external --provider-id <installed-provider-id> --secret-ref <opaque-vault-reference>
```

Set an external mode for one account by adding `--account-alias <alias>`.
The `store-testnet-credential` command writes through the selected provider;
it never changes the selected custody mode.

### Verification boundary

The current code tranche implements the profile database, router, adapter
contract/discovery, CLI configuration and deterministic fake-provider tests.
It does **not** claim that a concrete third-party vault is integrated, that
provider authentication UX is complete, or that any live exchange credential
has been imported. The exchange adapter remains Binance Futures Testnet only.
No live order, production credential, deployment, or activation is authorized
by this work.


### Mode changes and credential migration

Changing a global or per-account profile changes only the selected provider
route; it does not copy, delete, rotate, or revoke credentials in either store.
After selecting a new mode, the operator must provision the same exchange key
pair into the newly selected provider and run the signer's read-only startup
preflight. Only after the new provider is verified may the old copy be removed
or the exchange key rotated/revoked. A missing credential after a mode change
must stop signer startup; it must never trigger a search in the previous store.

## 17. First-party password-manager CLI adapters (2026-11-11)

The signer now includes first-party Bitwarden and 1Password provider factories
in aitos/native/cli_credential_vaults.py. They are registered by
discover_external_vault_backends(); no third-party Python package or provider
token is persisted by AITOS.

### Provider item layout

Both adapters require an operator-created item in the selected vault. AITOS
does not search the entire vault or create an item implicitly.

- Bitwarden: set the profile secret_ref to the exact item UUID. The item must
  contain custom fields named aitos_exchange_key_id and
  aitos_exchange_api_secret. The adapter writes both as hidden custom fields
  (Bitwarden field type 1).
- 1Password: set secret_ref to <vault-id>/<item-id>. The item must contain
  custom fields named aitos_exchange_key_id and aitos_exchange_api_secret.
  The adapter writes them as CONCEALED fields.

The credential router appends /key-id and /api-secret to this base reference.
The adapter rejects malformed references, mismatched item IDs, duplicate
field labels and malformed provider responses.

Configure an account after creating its dedicated item:

```bash
python -m aitos.security.signer_cli set-credential-mode --mode external --provider-id bitwarden --secret-ref <bitwarden-item-uuid> --account-alias primary
python -m aitos.security.signer_cli set-credential-mode --mode external --provider-id onepassword --secret-ref <vault-id>/<item-id> --account-alias primary
```

The CLI command changes routing metadata only. Provision the item fields and
run the signer's read-only credential preflight before allowing the signer to
accept intents.

### Session, lock and timeout behavior

- The signer invokes the installed bw or op executable without a shell.
- Provider CLI authentication remains the provider's responsibility (for
  example, an unlocked Bitwarden CLI session or 1Password desktop/service
  account session). AITOS does not prompt for, store, or log provider master
  passwords, session keys or service-account tokens.
- Each provider operation has an 8-second default process timeout. Non-zero
  exit, locked vault, expired session, missing executable and timeout all
  become redacted provider errors. No native-vault fallback is attempted.
- Secret-bearing JSON is passed through standard input for encode/edit
  operations; secrets are not placed in command-line arguments.
- An item reference must identify a dedicated AITOS credential item. Do not
  reuse an item containing unrelated fields or share it with untrusted users.

### Rotation and restart procedure

Do not rotate a key pair in place while the signer is accepting intents. Stop
the signer first. Create a separate new item in the selected provider, populate
the two AITOS fields, point the account profile at the new item, and run the
read-only signer preflight. Retain the old item until the new pair is verified
and the operator has reconciled outstanding orders. Only then remove the old
item or revoke the old exchange key. A partial provider write must be treated
as incomplete; it must not be interpreted as a successful pair rotation.

### Verification boundary

The dedicated tests use a deterministic fake subprocess runner to verify
argument construction, stdin handling, field mapping, malformed references,
duplicate fields and timeout redaction. They do not authenticate to real
Bitwarden or 1Password accounts. Real vault unlock/lock, provider-side
permission scopes, service-account/desktop-session behavior, network outages,
restart recovery and actual credential rotation remain cross-platform
acceptance tests. The desktop Settings screen is not yet connected to the
signer profile database; the adapters are currently configurable through the
signer CLI only.

No provider account was logged into, no exchange credential was imported, and
no exchange order or deployment was performed by this change.
