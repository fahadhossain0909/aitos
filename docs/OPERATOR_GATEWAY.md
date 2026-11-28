# Read-only operator gateway

Status: implemented and locally tested; **not deployed or production-certified**.

## Purpose and trust boundary

The built operator console is served through
`python -m aitos.console_gateway`. A configured operator identity and password
establish a short-lived pre-authentication state; a server-issued, user-bound
WebAuthn assertion is required before an authenticated HttpOnly session exists.
The browser never receives the backend bearer. There are no order, live
activation, configuration-application, credential-vault or job-launch routes.

This is not an exchange login. Never reuse an exchange credential as the operator
password. Do not paste passwords/tokens into chat, source files or command history.

Vite is now explicitly development-only: providing `AITOS_CONSOLE_TOKEN` makes it
fail closed, and dev requests strip authorization, proxy-authorization and cookie
headers. Earlier instructions to put the backend bearer in Vite are superseded.

## Local workstation runbook

These are local-workstation commands, **not instructions to attach real data to
Arena's public preview**. The current shared preview remains sample/unattached.
The protected gateway was exercised only inside isolated tests in this session.

1. Build in a clean terminal without a runtime bearer in the Vite environment:

   ```sh
   cd desktop
   npm ci
   npm run build
   ```

2. In a separate private terminal at the repository root, configure a strong,
   unique operator password of 24–1024 UTF-8 bytes using a password manager or a
   hidden shell prompt. Example for bash (does not put the value in history):

   ```sh
   read -rsp 'Operator password: ' AITOS_OPERATOR_PASSWORD; printf '\n'
   export AITOS_OPERATOR_PASSWORD
   ```

   The length check is not an entropy guarantee. Prefer a high-entropy generated
   credential, not a long predictable/reused phrase. The application stores a
   random-salted scrypt verifier in RAM, not a password file. Environment/process
   secrecy and the local machine are still part of the trust boundary; this is
   not an OS secret vault.

3. Start the already-approved **unattached** adapter separately if evaluating
   empty state only:

   ```sh
   .venv/bin/python -m aitos.console_api
   ```

4. Start the gateway locally:

   ```sh
   .venv/bin/python -m aitos.console_gateway \
     --origin http://127.0.0.1:8780 \
     --upstream http://127.0.0.1:8765 \
     --host 127.0.0.1 --port 8780
   ```

5. Open `http://127.0.0.1:8780`. Sign-in protects the read-only UI. Sample financial
   values stay labelled; an unattached backend still has no real positions or
   performance data. Signing in never activates a runtime.

For an already-approved attached backend, supply its existing long bearer as
`AITOS_CONSOLE_TOKEN` **only in the gateway environment**, and set `--upstream`
to the approved health-server origin. Do not enable live trading to obtain data.
Never place an operator password or backend token in a `VITE_` variable.

Direct `vite preview` is no longer a protected built-app deployment path: the
production frontend requires a valid gateway session schema. Only Vite DEV mode
recognizes its HTML-only session probe as an intentionally anonymous development
sandbox. Server-side financial authorization remains mandatory regardless of UI.

## Implemented controls

- Exact canonical public host and browser Origin checks; forwarded host/proto/IP
  headers do not establish trust. Cross-origin/same-site-other-origin browser
  access is denied. Non-loopback public/upstream origins require HTTPS.
- JSON-only sign-in with bounded bodies; WebAuthn ceremony bodies have a
  separate 96 KiB cap. Login, MFA and approval operations share a bounded
  per-process attempt limiter and serialized scrypt work.
- Per-identity scrypt verifiers with random 16-byte salts, N=16384/r=8/p=1 and
  constant-time digest comparison. Unknown identities execute a dummy KDF.
  No plaintext operator password is kept as gateway state.
- Password verification creates only a 180-second hash-only pending cookie.
  Session issuance occurs only after the user-bound WebAuthn assertion verifies;
  the pending cookie is consumed on success and process restart revokes all state.
- First authenticator enrollment on a non-loopback origin requires a
  per-identity, persistent one-time out-of-band bootstrap token. Additional
  authenticators require a fresh assertion from an
  already-registered authenticator. Credential inventory is user-scoped.
- Lost-key recovery is an offline host-administrator operation through
  `scripts/manage_webauthn_credentials.py`; no browser credential-delete route
  exists. Revoking the final key re-enters the guarded first-enrollment process.
- 256-bit random session tokens; only SHA-256 token hashes are stored server-side.
  Four sessions maximum by default; issuing beyond the cap revokes the oldest.
- Absolute 15-minute lifetime by default (configuration allows 60–3600 seconds).
  Status checks and automatic snapshot polling **do not renew** that lifetime.
- Cookie: HttpOnly, SameSite=Strict, Path=/, no Domain. HTTPS origins use Secure
  `__Host-aitos_operator`; local HTTP uses `aitos_local_operator` without Secure.
- Rotation on successful sign-in; logout invalidates the captured token; process
  restart discards all sessions. No session token is stored in local/session
  storage. Existing local configuration drafts remain unrelated, unapplied data.
- Only GET for four exact console routes: snapshot, history, performance and
  research plan. Route-specific allowed query names, no duplicate names and a
  2,048-character decoded-query bound. No arbitrary upstream URL/path or browser auth/cookie
  forwarding. The gateway adds only its own configured backend bearer.
- Four concurrent upstream slots, fail-fast overflow, four connector sockets,
  six-second total/two-second connect/five-second read timeouts and a 2 MiB body
  ceiling. Upstream cookies are discarded. Redirects are never followed.
- Identity encoding requested; unexpected compressed upstream bodies, wrong
  MIME/type, duplicate JSON keys and NaN/Infinity are rejected. Upstream failures
  are sanitized; backend auth errors are not mistaken for an operator login.
- Built files only, fixed root assets and approved asset suffixes; no directory
  listings, hidden files, source maps, traversal or symlink escapes.
- No-store responses, no CORS, nosniff, no-referrer, frame denial, restrictive CSP,
  limited browser permissions and HSTS for declared HTTPS origins. CSP allows
  inline styles because the current React chart/layout components use them;
  scripts remain self-only without unsafe-eval.
- Coarse accepted/rejected/revoked/failure audit messages only; no password,
  cookie, token, query string or raw upstream error logging by the gateway.

## Browser behavior

The production bundle checks the session before mounting financial views. Sign-in
has no exchange-key fields. A read-only session bar shows remaining absolute
lifetime and sign-out. Expiry/revocation (401) unmounts financial state; server
expiry is authoritative even if the browser clock is wrong.

Sign-out hides the financial UI immediately. If the network prevents confirming
server revocation, the screen explicitly warns that the session may still be
valid and offers retry. It never falsely reports successful logout. Other open
tabs may take until their next request to observe server-side revocation.

A gateway verification/network failure in a production bundle hides the
workspace. It does not silently fall back to anonymous development mode.

## Verification

```sh
.venv/bin/python -m pytest tests/test_console_gateway.py --timeout=30 -q
cd desktop && npm run test && npm run build
cd ..
AITOS_RUN_OPERATOR_BROWSER=1 .venv/bin/python -m pytest \
  tests/test_operator_gateway_browser.py -m integration --timeout=90 -q
```

The opt-in browser harness uses loopback TestServers, a built bundle, dummy
credentials and the unattached adapter only. A test-controlled monotonic clock
checks server expiry without waiting a real minute. It verifies sign-in failure,
authenticated reads, HttpOnly/SameSite flags, no credential storage, logout,
expiry clearing, failed-revocation warnings, retry, CSP and mobile layout.

`desktop/browserRuntime.mjs` prepares Linux NSS/NSPR libraries already bundled in
the locked Chromium npm dependency under ignored `tmp/`. It removes the relaxed
web-security and feature-disable flags from the headless package and does not pass gateway secrets
into Chromium's process environment. No system package installation is required
by this helper. Browser tests currently target Linux, not native OS certification. Headless CI
process/sandbox flags are not a certified native-browser security profile.

The operator-quality workflow runs these checks on relevant PRs and manual
execution, without deployment or real credentials. Treat each result as specific
to its exact commit SHA. The workflow does not certify external TLS termination,
hostile-network security or production account reconciliation.

## Approval control plane

The Approval Desk is enabled only when both `AITOS_APPROVAL_SIGNING_KEY` and
`AITOS_OPERATOR_ROLE_MAP` are configured on the gateway. The role map is a
server-owned JSON object mapping identities to role arrays; the default live
approval role is `live_trader`. A challenge binds the canonical action digest
to the authenticated identity and a WebAuthn authentication challenge. A fresh
user-verified assertion issues a short-lived, single-use signed approval receipt.
Audit rows are append-only. The browser displays the receipt ID, not the signing
secret.

The gateway deliberately does not execute or submit orders. A trusted
server-side receipt provider and exact-action, single-use consumer are
implemented in `LiveApprovalControlPlane` and
`BinanceFuturesOrderExecutor`. Entry and protective STOP/TP orders bind the
approval digest to the actual venue side, exact quantity, reference/trigger
price, client order ID, account scope and trade owner. The executor persists
the receipt ID, approver, digest and expiry in the durable execution-intent
request before the venue POST; the canonical OMS intent event then provides
the causal parent for the resulting Trade transition. The token itself and
signing key are never copied into the OMS event.

For gateway/runtime store sharing, both processes must use the same
`AITOS_WEBAUTHN_DB`, `AITOS_WEBAUTHN_ORIGIN`,
`AITOS_WEBAUTHN_RP_ID`, `AITOS_EXECUTION_SCOPE`,
`AITOS_APPROVAL_SIGNING_KEY` and `AITOS_OPERATOR_ROLE_MAP`. The gateway's
`--webauthn-db` and `--origin` must exactly match the shared environment
values. `AITOS_EXECUTION_SCOPE` must equal the runtime's SHA-256 account scope;
the approval desk displays it and rejects actions for another scope. For a
mainnet account, compute the scope without printing the API key:

```bash
python -c 'import hashlib,os; print(hashlib.sha256(("False:" + os.environ["BINANCE_API_KEY"]).encode()).hexdigest())'
```

Both processes derive the same sibling `live-approvals.sqlite` file. If the gateway and executor
run in separate containers, that file must be on one protected, durable,
shared volume with SQLite-compatible locking; separate ephemeral container
filesystems do not constitute a shared approval store.

Mainnet execution remains disabled by default. It requires
`AITOS_ENABLE_MAINNET_EXECUTION=1`, the shared approval configuration, the
interactive session confirmation, and a fresh single-use WebAuthn receipt
for every entry or protective order. The code does not set this flag or
perform live execution as part of CI. An issued receipt is not execution
confirmation; external account reconciliation and independent production
acceptance remain mandatory.

## Limits and nonlocal deployment gate

- No built-in TLS listener: external HTTPS termination must preserve the exact
  configured Host and enforce trusted routing. Forwarded headers are ignored.
  A configured HTTPS origin alone is **not proof** of deployed TLS correctness.
- Local HTTP is loopback-only and uses a non-Secure cookie. Never expose it as an
  Internet login. Do not reuse operator credentials across environments.
- Sessions and pending MFA state are single-process and volatile; no OIDC,
  distributed session revocation, self-service password reset, distributed
  identity federation or OS vault is included. The identity directory and role
  map are server configuration, not an external enterprise IdP.
- Global attempt limiting may temporarily block a legitimate operator under
  abuse; public deployments need independently reviewed edge protection.
- Secure flags were tested, and the real built browser flow ran locally over
  loopback HTTP. External TLS, hostile-network penetration and independent
  security review are **not certified**.
- Host/Origin checks and CSP are defense-in-depth, not substitutes for patching,
  safe dependencies, authentication or a hardened host. Existing unauthenticated
  health/metrics endpoints outside this gateway must remain private.
- Gateway credential/state changes must be reviewed; authentication does not
  satisfy account reconciliation, persistence, soak or live-readiness gates.

Stop the gateway to revoke all RAM sessions. Rotate a compromised operator
credential through the private server environment and restart; rotate a backend
bearer through the approved backend procedure. Never clear trading intents,
change risk policy or delete financial state to recover console access.
