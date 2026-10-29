# AITOS Codebase Structure and Duplication Audit

Status: initial static review; follow-up runtime and repository-wide checks required.

## Goals

Reduce accidental duplication and make module ownership easier to understand without changing trading behavior, event contracts, risk limits, persistence semantics, or runtime entrypoints.

## Initial findings (confirmed from current `main` files)

1. **Runtime entrypoint is a large composition root.** `run_paper_trading.py` imports configuration, market data, persistence, learning, execution, monitoring, and explainability components and defines operational constants. Some of this is appropriate for orchestration, but the file should be checked for business logic that belongs in dedicated services. Do not move code until imports and lifecycle ordering are mapped.
2. **Tuning environment-variable mapping is explicit and lengthy.** `aitos/config/tuning.py` contains typed Pydantic settings plus a large environment-variable-to-field mapping. The mapping is not automatically a defect: explicit whitelisting provides validation and avoids reading unrelated variables. Consider table-driven declarations only if tests prove behavior remains identical.
3. **The repository has many long-lived branches.** The branch list includes several similarly named market-data, order-book, Redis archive, position-recovery, and observability branches. Similar names are not proof of duplicate changes; compare each branch with `main` and check associated PRs before deleting anything.
4. **Duplicate basenames are not automatically duplicate implementations.** A recursive Git tree inventory found 701 tracked files and repeated generic names such as `engine.py`, `models.py`, `contracts.py`, `repository.py`, and `__init__.py` across domain packages. These are mostly domain-local modules, not safe candidates for consolidation based on filename alone. The two Binance adapter test files also cover different layers: the exchange protocol adapter versus the canonical market-data adapter.
5. **Unreferenced root placeholders were present.** `tmp.txt`, `tmp2.txt`, and `tmp3.txt` each contained only `x`; repository search found no references. They were removed on this refactor branch.
6. **Existing architecture boundaries should be preserved.** Current package areas include configuration, data, exchange adapters, execution, intelligence, learning, market-data, trading state, and explainability. Avoid broad folder moves until import references, packaging, Docker, CI, scripts, and docs have been inventoried.

## Refactoring plan

### Phase 1 — inventory and evidence
- Enumerate tracked files by package and file type (initial recursive tree inventory: 701 tracked files).
- Treat duplicate basenames as review signals, not proof of duplicated logic; compare contents and responsibilities before moving or deleting.
- Identify exact duplicate files and high-similarity functions; separate generated/vendor/test fixtures from production code.
- Map entrypoints, imports, plugin/registry loading, event subscriptions, configuration sources, and persistence owners.
- Compare branch diffs against `main`; classify as merged-equivalent, unique useful change, obsolete, or unsafe/unknown.
- Record baseline test/lint/type-check results before production refactors.

### Phase 2 — safe deduplication
- Extract shared logic only where implementations have the same contract and failure semantics.
- Prefer one authoritative source for paths, ports, configuration defaults, retry policy, and serialization schemas.
- Keep exchange-specific protocol adapters separate when behavior genuinely differs.
- Keep test helpers in a shared test utility only when multiple suites use the same stable contract.
- Add regression tests before replacing duplicate implementations.

### Phase 3 — module ownership and layout
- Keep the existing importable `aitos` package stable initially.
- Group new code by domain responsibility rather than file type.
- Move files only in a dedicated change, updating Python imports, tests, Dockerfiles, Compose files, workflow paths, entrypoint commands, docs, and any dynamic imports.
- Use temporary compatibility re-exports only where external/internal consumers need them; remove them after verified migration.

### Phase 4 — validation and cleanup
- Run formatting, lint, static checks, focused tests, then the full test suite.
- Run service-backed tests with isolated test credentials and services.
- Smoke-test paper mode only; never use real exchange credentials or live orders as a refactoring test.
- Re-run checks after every fix and compare runtime configuration and event contracts.
- Delete branches only after confirming their useful commits are merged or intentionally abandoned and no open PR depends on them.

## Acceptance criteria

- No unexplained duplicate production implementation remains.
- No broken imports, entrypoints, config paths, Docker/CI references, or documentation links after any move.
- Existing risk and trade lifecycle behavior remains unchanged unless a separately tested bug fix is justified.
- Tests and static checks are green, with skipped/unavailable integration checks explicitly reported.
- No branch is deleted solely because its name resembles another branch.

## Important limitation

This document records the initial findings from files and GitHub metadata available during this review. It is not a claim that a complete clone, exhaustive duplicate-code scan, or full runtime test suite has already run. Structural moves and branch deletion must follow repository-wide inventory and validation, not assumptions based on names.
