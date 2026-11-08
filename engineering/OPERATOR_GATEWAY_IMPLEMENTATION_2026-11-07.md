# Authenticated read-only operator gateway — 2026-11-07

## Outcome and scope

**VERIFIED:** a local-first single-operator gateway and built-console sign-in,
absolute expiry and logout flow now exist. Runtime bearers remain server-side.
The unsafe development pattern of supplying that bearer to Vite is rejected.
The existing nine-page workspace still passes its browser checks.

**NOT TESTED:** production deployment, external TLS termination, independent
penetration review, native operating systems and real-account use. This is not
production security certification, MFA, a native vault or a trading mutation
control plane. The shared development preview remains unattached. No long-lived
gateway or real operator credential was created for that preview.

The approved continuation is implemented on `arena/ed127d35-aitos`, preserving the
pre-existing broad dirty tree. No commit/push, real order, live activation,
deployment, migration, destructive cleanup, strategy or risk-policy change.

## Implemented change

### Backend and authorization

`aitos/console_gateway.py` uses existing aiohttp infrastructure in a separate
read-only process. It validates exact public/upstream origins, rejects nonlocal
HTTP and self-proxying, and serves an allowlisted production bundle.

Operator configuration requires a 24–1024-byte password. State holds a random
salt and scrypt verifier, not the plaintext password. Sign-in work is serialized
and attempt-limited. Random 256-bit session tokens are stored only as SHA-256
keys, have monotonic absolute expiry and a bounded inventory. Logout/rotation,
expiry and process shutdown revoke relevant state. Restart resets both sessions
and rate-limit history; it is not distributed identity infrastructure.

Host/Origin/fetch-site checks, JSON-only POST, body limits, HttpOnly Strict cookies,
HTTPS Secure/__Host cookies, HSTS, CSP, no-store and framing restrictions provide
defense in depth. The loopback HTTP cookie is deliberately named separately.
Cookie deletion uses `set_cookie` flags compatible with the declared aiohttp
minimum rather than relying on newer `del_cookie` keywords.

Only the four exact read APIs are proxied, with method/query allowlists and no
browser credential/header forwarding. Upstream I/O has a bounded connection
inventory, fail-fast concurrency, timeouts, a body ceiling, no redirect following,
no cookie persistence and strict uncompressed JSON-object parsing. Duplicate
keys, nonfinite constants and malformed/oversized responses fail closed. Errors
and audit messages do not echo upstream secrets or raw payloads.

### Frontend and developer boundary

`OperatorBoundary.tsx` mounts financial views only after a valid session response
in production. It adds a responsive sign-in page and a read-only expiry/logout
strip. `operatorSession.ts` centralizes same-origin, no-store console requests and
401 invalidation across App, PerformancePanel and ResearchPlanner.

Expiry or revocation unmounts the workspace. Logout hides it immediately; an
unconfirmed network-side revocation is clearly warned and retryable, rather than
reported as completed. Password input is memory-only and cleared; session tokens
are HttpOnly and not stored in browser storage. Server expiry remains authoritative.

Vite now rejects `AITOS_CONSOLE_TOKEN` and strips browser authorization,
proxy-authorization and cookie headers. Only DEV mode recognizes Vite's HTML
session probe as an unattached development sandbox; a production HTML/error
response cannot silently unlock financial views.

### Reproducible verification tooling

- 52 isolated gateway tests cover authentication, expiry/rotation/caps/rate limits,
  CSRF/Host checks, path/method/query boundaries, HTTPS cookie/HSTS attributes,
  secret-safe errors/logging, static restrictions and upstream resource bounds.
- Ten frontend session/schema tests and three Vite security tests were added.
- An opt-in real Chromium harness serves the built bundle through isolated
  loopback TestServers and uses dummy credentials plus a test-controlled clock.
  It exercises rejected sign-in, successful sign-in/reads, cookie flags, no token
  storage, revocation, absolute expiry clearing, failed-logout warning/retry,
  CSP/runtime errors and mobile overflow.
- The bundled headless Chromium defaults contained relaxed web-security flags.
  They are removed from both browser harnesses; authentication checks must not
  rely on `--disable-web-security`. The shared Linux test helper also prepares
  bundled NSS/NSPR libraries under ignored `tmp/` without a manual system install
  and removes server credential environment variables from the Chromium process.
- A least-privilege, no-deployment `operator-quality.yml` workflow now defines
  relevant PR/manual checks. **NOT TESTED:** its remote GitHub execution.

## Final observed verification

| Check | Evidence |
|---|---|
| Backend nonintegration regression | **1783 passed, 22 deselected, 87.13s** |
| Dedicated gateway suite, included above | **52 passed, 4.60s** |
| Separate embedded ClickHouse regression | **6 passed, 1.46s**; local embedded engine, not server transport |
| Frontend Vitest | **64 passed**, six files |
| Production TypeScript/Vite build | **VERIFIED** successful |
| npm audit | **VERIFIED: 0 vulnerabilities reported** at this run; not a security certificate |
| Existing nine-page browser | **VERIFIED** planner/export, performance fixtures, readiness, runtime/sample separation, dialogs, mobile, failure clearing, no JS errors |
| Built operator Chromium flow | **1 passed, 14.33s**, no custom LD_LIBRARY_PATH required |
| Focused Black/F-lint/compile | **VERIFIED** for gateway and its Python tests |
| Focused Bandit | **VERIFIED** no findings for gateway source |
| pip check / git diff --check | **VERIFIED** no dependency conflicts / whitespace errors |

Backend log: ignored `tmp/backend-gateway-final.log`. Auth browser log:
`tmp/operator-browser-final.log`. Build log: `tmp/console/gateway-build.log`.
The browser test ran concurrently with other validation; timings are test elapsed
time, not measured production latency or capacity/SLO evidence.

Login screenshots: `tmp/console/operator-login.png` and
`tmp/console/operator-login-mobile.png`. They show only the isolated sign-in UI,
not real accounts, funds or trading performance. Existing analytics screenshots
remain synthetic fixtures as previously documented.

## Runbook, rollback and remaining gates

See [`docs/OPERATOR_GATEWAY.md`](../docs/OPERATOR_GATEWAY.md) for exact local
commands, credential handling, controls and deployment limitations. The old
bearer-in-Vite instructions are superseded, including for private development.

No deployed service needs rollback. To stop an evaluation, stop the gateway;
all RAM sessions are lost. Rotate compromised credentials only through the
approved private server environment/backend procedure. Do not restore the old
bearer-in-development-proxy behavior, bypass financial readiness gates, clear
intents, delete ledger records or change risk settings to recover UI access.

Next backend release work remains strict monotonic persistence revisions/fencing,
real-service restart/transport and account reconciliation, followed by controlled
paper soak/end-to-end latency evidence. Product delivery also still needs a
reviewed native vault/installer/updater and audited mutation/job control plane.
Gateway authorization does not grant any of those capabilities. Existing
readiness gates deliberately remain un-certified; no profitable-edge or ranking
claim is justified by this increment.
