# AITOS System Audit — 2026-11-20

## Executive status

- Repository: `fahadhossain0909/aitos`
- Audited baseline: `main` at `b71ab51328330120c2028accc1e7f85607db0b74` (20 November 2026 06:24:37 UTC).
- Recent merged integrations confirmed from commit history:
  - PR #259 — Binance Spot integration (`27444433973577e732bb5a230e3b82ae29da0408`).
  - PR #260 — Architecture Evolution Program and risk/market contract work (`88083953931ea2f87c247df96c72d53d2be191e0`).
  - PR #261 — Adaptive hardware capacity foundation (`bb2aa7f67ad94c9c2953ae0ee1613e4f263befbd`).
- A follow-up formatting/safe-lint commit is on main after PR #261.
- This is a source/documentation/workflow audit. No local AITOS runtime, exchange account, VPS, or production data was accessed. Current-main CI and runtime certification are not claimed.

## Evidence vocabulary

- **SOURCE-VERIFIED** — the relevant source/configuration is present on the audited main head.
- **DOCUMENTED** — repository documentation describes the behavior, but this audit did not independently execute the runtime path.
- **NOT VERIFIED** — no current-head test/runtime evidence was available in this audit.
- **OPEN GATE** — must be completed before production or live activation.

## What is present on main

| Area | Finding | Evidence status |
|---|---|---|
| Futures market/runtime | Existing Binance USD-M Futures adapters and paper/live entrypoints | SOURCE-VERIFIED; venue behavior NOT VERIFIED |
| Spot | Separate Binance Spot runtime and market-data adapter; USDT quote scope | SOURCE-VERIFIED |
| Spot safety defaults | Spot disabled; testnet true; execution disabled; mainnet not allowed by default | SOURCE-VERIFIED in `docs/SPOT_MARKET_INTEGRATION.md` |
| Market architecture | Canonical market-data envelope, separate asset-neutral domain contracts and provenance bridge | SOURCE-VERIFIED; production shadow-consumer wiring remains an explicit gate |
| Portfolio/risk | CapitalGateway, atomic reservation checks and pending-order exposure projection additions | SOURCE-VERIFIED; complete provider freshness and all-path runtime proof remains open |
| Execution recovery | Durable intent/quarantine and protection reconciliation mechanisms | SOURCE-VERIFIED; protective POST-to-ACK recovery and full terminal history remain open |
| Compute | Adaptive capacity detection, bounded optional-work admission and queue pressure telemetry | SOURCE-VERIFIED; disabled by default and rejected in live mode |
| Rust | Optional order-book replay/native path | Must remain opt-in until native parity and end-to-end evidence are independently checked |
| Frontend | Ten-page React/TypeScript operator workspace and typed response validation | SOURCE-VERIFIED; exact-main browser runtime certification NOT VERIFIED |
| Desktop | Native Windows/Linux session manager plus separate Tauri embedded shell | SOURCE-VERIFIED; these are distinct delivery tracks |
| Infrastructure | Docker Compose, Redis, ClickHouse, Neo4j, optional workers and guarded CD | SOURCE-VERIFIED; no current-host deployment/soak was run |

## Findings and priority

### P0 — Exchange credential custody and live-order trust boundary

The deployment workflow can pass optional `BINANCE_API_KEY` and `BINANCE_API_SECRET` into the VPS runtime environment. The credential-security RFC correctly explains that a sufficiently privileged administrator of that same host can inspect process memory, files, environment and binaries. The separate remote signer/live dispatch boundary is not production-implemented or certified.

**Required action:** keep provider-hosted live execution disabled when the host administrator is outside the trust boundary. Do not treat OS vault storage on that same host as protection from its administrator. Complete signer-side authorization, replay protection, durable audit, account/protection reconciliation, independent security review and explicit approval before live activation.

### P0 — Protective-order crash window

The Architecture Evolution Program and execution-recovery documentation retain an unresolved protective-order POST-to-acknowledgement / persistence crash window, with automated recovery and full terminal-history reconciliation still incomplete.

**Required action:** preserve fail-closed quarantine and no-blind-resubmit behavior. Add deterministic fault injection for crash-before-ack, lost acknowledgement, duplicate acknowledgement, partial child fills, cancellation/fill races and restart recovery. Do not relax the gate based only on offline read-only reconciliation.

### P1 — Desktop delivery ambiguity and local build instructions

Two different desktop tracks exist:
1. Native Windows/Linux browser session manager, which launches the read-only console in the default browser and connects to an existing backend.
2. Tauri embedded desktop shell, which packages the React workspace, a gateway sidecar and a separate local Trusted Signer Settings window.

The installation guide previously described the session manager as the desktop application without explaining the Tauri candidate. The Tauri local build instructions omitted the required PyInstaller sidecar build, even though `tauri.conf.json` declares that sidecar as an external binary.

**Remediation in this branch:** clarify the two products, their release status and security boundaries; add a complete host-native sidecar build sequence and exact CI artifact names.

### P1 — Desktop validation did not re-run on main

The console-quality, Windows-console and Linux-console workflows had push triggers pinned to the arena branch. The Tauri workflow was PR/manual only. Relevant PR checks existed, but post-merge mainline validation was not configured for these workflows.

**Remediation in this branch:** add path-filtered main push validation to all four desktop workflows while retaining the arena branch trigger where it existed. Add a regression test that protects the mainline trigger policy.

### P1 — Financial source-of-truth and reconciliation matrix

The architecture program explicitly records that financial state is distributed across ClickHouse, Redis, SQLite/scoped journals and other stores, with no single cross-database transaction. The authority matrix for orders, fills, positions, cash, fees, funding, reservations and model artifacts is not yet complete.

**Required action:** define one authoritative writer/reader and reconciliation source per aggregate; specify idempotency, transaction boundary, replay behavior and recovery procedure. Do not silently repair discrepancies or release pinned capital when evidence is ambiguous.

### P1 — Spot rollout remains intentionally limited

The Spot adapter is present, but it is a separate market domain. The current documented rollout is USDT-quoted only, testnet-first, disabled by default, with execution disabled and mainnet disallowed by default. Futures-only funding/OI/liquidation fields must not be fabricated for Spot. Futures and Spot account equity must not be double-counted.

**Required action:** retain these defaults until Spot testnet order lifecycle, process-restart recovery, fee-asset conversion, portfolio valuation and cross-market OOS lead-lag tests are verified on a controlled environment.

### P1 — Runtime and production evidence is older than current main

The repository contains hosted validation reports for earlier source SHAs, including Windows install/uninstall and service restart/soak runs. Those reports are useful historical evidence but predate the latest main integrations. The current audit did not obtain a successful workflow result for the audited main SHA, nor run a paper soak, exchange testnet lifecycle, Windows/macOS/Linux physical-device acceptance, or restore drill.

**Required action:** create a commit-pinned acceptance run for current main and the final candidate. Record exact SHA, workflow run, artifact digest, test selection, environment and explicit limitations. Never transfer a PASS from an older SHA to a newer one.

### P2 — CI quality gates can be strengthened

The main CI workflow currently:
- tests Python 3.10, 3.11 and 3.12 with Redis, ClickHouse and Neo4j services;
- runs Black and a Flake8 critical-error subset (`E9,F63,F7,F82`);
- generates coverage artifacts but does not enforce a minimum coverage threshold;
- installs Pylint but does not run it;
- runs Bandit and Safety checks and validates/builds the Docker image.

**Required action:** introduce a measured, staged coverage threshold; run a maintainable full lint policy (for example Ruff or a scoped Flake8 profile) without enabling a repository-wide noisy gate; add container SBOM/vulnerability and provenance checks; add migration/restore and contract compatibility gates. Do not turn on a strict global lint threshold without first baselining existing violations.

### P2 — Documentation freshness and product truth

README previously described Futures as the only exchange and contained branch-specific integration statements. Desktop and installation docs did not clearly distinguish the two desktop products. Historical validation reports could be mistaken for current-main certification.

**Remediation in this branch:** update the README's product scope, capabilities, desktop tracks, architecture links and historical-evidence caveat; update installation and Tauri build instructions; add this audit report.

## Safe changes made in this audit branch

- README updated to describe the merged Spot integration, shared market/risk architecture and opt-in adaptive compute without claiming production readiness.
- Desktop delivery tracks distinguished; Tauri candidate limits and credential trust boundary documented.
- Installation guide now explains how to obtain and build the Tauri candidate, including the required host-native PyInstaller sidecar.
- Console-quality, Windows-console, Linux-console and Tauri packaging workflows now run on matching changes pushed to `main`, in addition to their relevant PR/manual paths.
- A regression test protects those post-merge mainline validation triggers.
- No trading strategy, risk-limit, order-routing, credential, database schema, production deployment or live-execution behavior was changed.

## Acceptance gates still open

1. Full current-main backend CI and focused market-data, Redis, database, security and backtest workflows.
2. Frontend unit tests, production build, dependency audit and authenticated browser smoke on the same SHA.
3. Windows/Linux session-manager install, launch, backend attach, credential vault, upgrade/uninstall and clean-machine tests.
4. Tauri Windows/Linux/macOS build and packaged smoke on the same SHA; signed release remains separate.
5. Cross-store financial authority and crash/recovery matrix.
6. Futures and Spot testnet lifecycle/restart/fill/fee reconciliation; no live orders.
7. Paper-only soak with real service restart, queue lag, memory bounds and position/protection reconciliation.
8. Backup/restore drill and release rollback validation.
9. Independent security review of gateway, desktop signer settings, remote signer boundary and deployment secrets.
10. Walk-forward/regime OOS, execution-cost replay and champion/challenger governance evidence.

## Explicit non-claims

This report does not certify profitability, production capacity, latency percentiles, 24/7 reliability, power-loss durability, high availability, external TLS, signed release trust, real-account reconciliation, exchange testnet execution, or live-trading readiness.
