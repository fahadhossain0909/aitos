# Backend audit — 2026-11-07

## Executive status

Initial backend correctness package, on `arena/ed127d35-aitos`, rebased by safe
fast-forward to main `040fff9c660212b78c6844f82e9f878f4a0032d7`.
No runtime service was activated or restarted. No exchange credentials/orders,
strategy/risk threshold changes, Redis tuning, database migration or deployment.
Backend certification is NOT complete. See `BACKEND_FIRST_ROADMAP.md` for package gates
and the desktop product/control blueprint.

## Evidence collection

- Working tree was clean before synchronization; branch remained unchanged.
- Initial shallow history prevented fast-forward with `refusing to merge unrelated
  histories`. `git fetch --unshallow origin` restored ancestry; `git merge --ff-only
  origin/main` succeeded. No unrelated-history merge, reset or branch switch used.
- Reviewed changes since `6f8052bb`, PR #243/#245 status and open #244/#246 scope.
- Inspected experiment/validation/walk-forward, pipeline/registry/governance/rollout,
  journal/recorder/worker call paths and app/runner initialization boundaries.
- Installed test dependencies into ignored local `.venv`; artifacts/logs in ignored
  `tmp/`. Python 3.11.2. No claim of Python 3.10/3.12 verification.

## Reproduced findings and bounded fixes

### AUD-20261107-01 — Development evaluation contaminates frozen strategy (P1 research integrity)

Status: ROOT CAUSE CONFIRMED; fix regression-verified locally.

`CandidateValidator` passed the caller's strategy object directly to the development
backtest. A stateful callable accumulated nine development timestamps, then
`WalkForwardValidator` deep-copied that already-mutated object. Recorded initial state
sizes were `[0, 9, 9, 9, 9]`, not `[0, 0, 0, 0, 0]`. Development includes data later
used in walk-forward windows, so this can contaminate supposed unseen-window evidence.

Fix: deep-copy the development-run strategy, leaving the frozen proposal untouched for
window/holdout copies. Also applies when walk-forward is disabled. No trading rule or
validation threshold altered. Tests include the real canonical engine with a no-order
stateful callable and an instrumented engine verifying every run's starting state.

Limitations: Python function closures/globals are not isolated by `deepcopy`; strategy
implementations must encapsulate mutable state in copyable instances. This patch does
not certify external fitted-model provenance or make arbitrary callbacks pure.

### AUD-20261107-02 — Experiment cells share mutable nested data (P1 research integrity)

Status: ROOT CAUSE CONFIRMED; fix regression-verified locally.

`list(regime_events)` copied only the outer list. A baseline evaluator annotating a
nested price list changed the next ablation's input and the caller's history. Dataset
fingerprint generation happened after evaluation, so mutation could also affect lineage.

Fix: deep-copy each cell's event slice before passing it to the evaluator. Tests check
baseline/ablation input equality, caller immutability and fingerprint/ID equality with a
non-mutating evaluator. Offline memory/CPU cost grows with slice size; large-dataset
optimization needs profiling, not shallow-copy fallback. Custom `__deepcopy__` semantics
and callback-global state remain caller responsibilities.

### AUD-20261107-03 — Experiment identity and scenario keys ambiguous (P2 auditability)

Status: ROOT CAUSE CONFIRMED; fix regression-verified locally.

Changing `minimum_samples` changed pass/fail while preserving the experiment ID. Two
cost scenarios could share a name even though baseline/delta indexing uses that name.

Fix: include sample acceptance policy in experiment identity and reject duplicate cost
scenario names. Existing experiment IDs intentionally change; old reports must remain
historical artifacts, not be silently relabeled. This is not a complete code/model/data
provenance registry. Canonical evaluator wiring remains a separate work item.

### AUD-20261107-04 — Test dependency profiles overwrite the same import (P2 verification reliability)

Status: ROOT CAUSE CONFIRMED; focused tests verified after clean reinstall.

`requirements.txt` used `aioresponses-ng`, whereas `requirements-backtest.txt` installed
legacy `aioresponses`. Both distributions export `aioresponses`; pip's dependency solver
does not detect file ownership collisions. Installing both left HTTP mocks failing with
`ClientResponse.__init__() missing ... stream_writer` under aiohttp 3.14.5.

Fix: use `aioresponses-ng>=0.8.2,<1` in the backtest profile as well. Add profile contract
tests. Repair an already-collided environment by uninstalling BOTH distributions then
installing the maintained fork; merely changing the file does not repair local modules.
After this local repair all 47 adapter/executor/live-preparation mock tests passed.
These tests mock order submission; no real order was sent.

## Wiring gaps / pending evidence

- VERIFIED: continuous intelligence/drift and rollout/experiment facilities are callable
  libraries; reviewed runners do not schedule reporting/canary collection or supply
  authenticated rollout adapters. These are intentionally not activated by this audit.
- VERIFIED: control-plane operator authentication is not supplied by approval envelopes;
  HealthServer is a diagnostics server, not a secure mutation API.
- VERIFIED: current experiment evaluator is injected by the caller; canonical-engine
  use and fitted-model/dataset lineage are not enforced by that callback type.
- HYPOTHESIS: callback-before-journal-write crash windows in rollout may require durable
  intent/reconciliation. Failure-injection/restart/concurrent-champion tests are needed
  before claiming reliable deployment; no router was activated here.
- NOT MEASURED: latency, market data completeness, actual fills, durable learning outcome
  coverage, profitability, paper soak, crash recovery and backup restoration.
- BLOCKED: actual ClickHouse-backed wiring verification; no local ClickHouse server.
- Dependency/runtime warnings about watchdog coroutine lifecycle remain an investigation
  item, not a confirmed production failure.

## Verification record

Commands below use the ignored `.venv` Python; no secrets or actual exchange services.

| Check | Observed result |
|---|---|
| New isolation regressions before fix | 5 failed in 0.14s (expected RED evidence) |
| Learning suite after research fixes | 36 passed in 0.23s |
| First broad run, `pytest -q -m 'not integration' --timeout=60` | 38 failed, 1289 passed, 2 deselected in 70.21s; HTTP mock collision plus unavailable ClickHouse |
| Focused adapter/executor/preparation after dependency repair | 47 passed in 0.37s |
| Learning + dependency contract tests after final additions | 39 passed in 0.26s |
| Black check of changed Python files | PASS — 4 files unchanged |
| `pip check` | PASS — no broken requirements |
| `git diff --check` | PASS |
| Final broad rerun, same non-integration selection | 7 failed, 1323 passed, 2 deselected, 9 warnings in 65.02s; all seven failures are ClickHouse connection-refused in app-wiring tests |
| New regression-only rerun | 8 passed in 0.08s (six research-isolation cases and two dependency-profile cases) |

The `not integration` marker does not exclude every database-dependent test: several
`tests/test_app_wiring.py` tests initialize a real `DecisionJournalRepository` without
an integration marker and fail connecting to localhost:8123. Do not silently skip them
or convert them to passing mocks and claim end-to-end coverage. A separate fixture/marker
contract and real-service CI run are required.

## Changed files

- `aitos/learning/validation.py`: frozen development strategy isolation.
- `aitos/learning/experiment_engine.py`: isolated nested cell inputs, unambiguous keys/ID.
- `requirements-backtest.txt`: consistent maintained HTTP mock dependency.
- `tests/learning/test_research_isolation.py`: state/input/identity regressions.
- `tests/test_dependency_contracts.py`: requirements-profile regression.
- `engineering/BACKEND_FIRST_ROADMAP.md`: revised delivery gates and desktop blueprint.
- This report plus append-only updates to progress/register/verification documentation.
- `docs/CONTINUOUS_INTELLIGENCE.md`: input isolation contract and compatibility notes.

## Rollback and remaining risks

Revert only this audit package's diffs, retaining the fast-forwarded main baseline and
all user data. No schema/data rollback is needed. Reverting research isolation restores
known contamination risks; reverting dependency alignment may restore install-order bugs.
No commit, push, merge to main or deployment is performed by this package.

Next: service-backed lifecycle/recovery evidence and test isolation classification;
quant metric/cost/provenance audit with coordination against open PR #246. Frontend
implementation remains downstream of relevant backend/control-plane gates.
