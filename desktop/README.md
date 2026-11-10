# AITOS Operator Console — local-first preview

Seven-page React/TypeScript workspace: overview, portfolio, trade journal,
research lab, observability, connections and configuration. Self-hosted fonts,
custom SVG charts and responsive olive/ivory/lime styling; no external CDN.

## Run safely

From the repository root, start the **unattached**, read-only adapter:

```sh
.venv/bin/python -m aitos.console_api
```

In another terminal:

```sh
cd desktop
npm ci
npm run dev -- --port 5173
```

Open port 5173. The initial **Sample workspace** is explicitly illustrative.
Switch to **Runtime view**: an unattached adapter has empty financial state,
not sample positions. API failures clear snapshots instead of retaining stale
financial records. Polling is sequential, cancellable and bounded to 8 seconds.
The browser uses same-origin `/console` requests, never browser localhost for a
separate backend.

## Attach an already-approved runtime

The existing paper/live health server registers console routes only when
`AITOS_CONSOLE_TOKEN` is supplied. Attached access requires a token of at least
32 characters. **Do not give this token to Vite**: the dev server/build now refuses
that environment variable and its proxy strips browser authorization/cookies.
Use the separately authenticated built-console gateway instead, with the backend
token only in that gateway's server environment. See
[`docs/OPERATOR_GATEWAY.md`](../docs/OPERATOR_GATEWAY.md).

Do not use a `VITE_`-prefixed secret, commit credentials, paste passwords/API keys
into chat, or put exchange API keys in this UI. Do not start live trading to test
the console. The public development preview remains an unattached/sample sandbox,
not a route to real financial data.

**Security boundary:** the new gateway implements single-process read-only
operator authentication, not a production control plane, OS vault, multi-user
identity system or security certification. TLS termination, hardening and
independent review are still required before any nonlocal activation.

## Implemented interactions

- Navigable pages, exchange-context dropdown, paper/backtest workspace tabs.
- Position detail dialogs, searchable/direction-filtered journal and CSV export.
- p50/p95/p99 stage selection; unmeasured values stay unavailable.
- Bounded local configuration drafts, device save and unapplied JSON export.
- Explicit gates for live activation, accounts, credentials and job launch.
- Keyboard search (Ctrl/Cmd K), Escape, dialog focus containment/restoration.

Exchange/mode selection is workspace context, **not runtime mutation**. Drafts do
not apply configuration. Connections do not store keys or activate accounts.
The recorded position P&L is the lifecycle record, not an independent mark-price
valuation. Runtime account-equity curves, drawdown and market prices are not yet
attached. Journal totals cover only loaded rows, not all-time account P&L.

## Verification

```sh
npm run test
npm run build
npm audit
npm run test:browser  # requires running UI/adapter and Chromium shared libraries
```

`smoke.mjs` uses packaged Chromium. On minimal Linux, its `libnspr4`/NSS runtime
libraries may be absent. The sandbox smoke used the libraries shipped inside
`@sparticuz/chromium/bin/al2023.tar.br`, extracted outside source and supplied
through `LD_LIBRARY_PATH`; normal desktops may already provide them.

## Native desktop installation

Native Windows x64 and Linux Debian-family packages are now built by the
repository's packaging workflows. They are read-only session managers that bundle
the console frontend, open it in the default browser, and connect to an existing
backend. They do not install or start AITOS trading services. Windows stores
operator/backend credentials in Windows Credential Manager; Linux requires an
unlocked Secret Service keyring. Both fail closed if the OS vault is unavailable.

Current packages are **unsigned test software**, not signed/trusted releases.
There is no automatic updater, macOS installer, one-click backend bootstrap or
Tauri-based installer claim. See the canonical [Installation & Deployment
Guide](../INSTALLATION.md) for artifact retrieval, build prerequisites, step-by-step
Windows/Linux installation, local backend setup and SSH-tunnel access to a VPS.

The installable-web manifest/service worker remain a browser starting point only;
no financial/API responses are cached and offline functionality is not provided.
`vite preview` does not provide the development API proxy; a production server
must implement secure routing/authentication.

## Quality workspace update (2026-11-07)

There are now **eight pages**, including **Quality & readiness**. This page reads
actual adapter observations even when the financial Sample workspace is active.
Its check counts are not a production grade. Expanding a check explains evidence
and next actions; the report can be exported. Live authorization is always false.
The engine is deterministic; no LLM provider has been integrated.

Snapshot/history responses now cross a typed runtime validator, not only a
TypeScript assertion. Snapshots older than 15 seconds or more than 5 seconds in
the future are rejected; clock skew must be corrected, not silently ignored.
On snapshot failure, financial rows and any open position details are cleared.
The journal has Previous/Next batch navigation, bounded to existing API offsets;
search and P&L totals cover only loaded rows. Offset pagination can shift while
the ledger changes and is not a transactionally consistent snapshot. Unknown
P&L totals remain unavailable, rather than inferred as zero.

The declared backend mode is now supplied explicitly by canonical runners;
unknown mode stays unknown rather than defaulting a live process to paper.
Workspace mode controls still do not activate or change that runtime.

The new `.github/workflows/console-quality.yml` runs frontend unit contracts,
build and audit on relevant PRs or manual dispatch. It never deploys or launches
trading. Its remote CI execution has not yet been observed.

## Research + ledger performance update (2026-11-07)

The console now has **nine pages**, including **Performance**. This page always
queries actual closed-ledger analytics, even in Sample workspace. An unattached
adapter displays unavailable—not synthetic returns. It supports refresh/export,
a cumulative recorded-PnL chart with keyboard slider, direction/strategy
attribution, cost-coverage cards and explicit financial limitations.

The source window is at most 1,000 **latest ledger updates**, then ordered by final
close time for display. It is not all-time account equity, mark-to-market
valuation or cash-flow timing. Unknown fields remain unavailable. Profit factor
is undefined without observed losses; no Sharpe or profitable edge is inferred.

Research Lab now opens a functional **Evaluation planner**. It computes bounded
rolling training/test date partitions and a locked holdout, with a pre-test gap
of label horizon plus buffer. It does not load a dataset, execute a backtest,
change strategy policy or promote anything. An optional dataset hash is a caller
declaration, not verified provenance.

New read-only endpoints:

- `GET /console/v1/performance` — fixed bounded, authenticated when attached.
- `GET /console/v1/research/plan?start=YYYY-MM-DD&end=YYYY-MM-DD` — bounded metadata
  computation, optional train/test/holdout/horizon/buffer and dataset SHA fields.

Python/TypeScript contract fixtures live under `src/*.fixture.json` and
`fixtures/performance-browser.json`. They are not imported into the running app
as account data. Python producer tests and TypeScript consumer tests verify them.
Browser performance screenshots are explicitly watermarked synthetic fixtures.
The browser script is self-contained with respect to fixture data; it still
needs the running adapter/UI and Chromium shared libraries documented above.

## Persistence & recovery (tenth page)

The workspace now has **ten pages**. Persistence & recovery consumes the optional
authenticated snapshot projection observation. Without an explicitly attached
observer it shows unavailable, never demo counters. Stale/malformed/failure
evidence clears metrics/export. The explicit synthetic example and its exported
provenance cannot qualify as runtime evidence. There are no projector-start,
credential, migration or order controls. A local ACK is not production certification.

Verification: 84 frontend tests, production build, ten-page/mobile smoke, and an
actual isolated projector → API → gateway → built authenticated browser test.
Details/limitations: `../engineering/PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md`.
