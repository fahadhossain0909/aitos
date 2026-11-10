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
