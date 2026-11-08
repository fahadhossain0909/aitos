# Quant research, financial evidence and database-backed desktop increment

Date: 2026-11-07 · Branch: `arena/ed127d35-aitos`

## Delivery summary

This increment implements research date planning, closed-ledger financial
analytics, the ninth frontend page, actual embedded ClickHouse SQL verification,
closed-history integrity gates and bounded console query execution. It does not
claim a world's-best ranking, profitable strategy, production-ready backend or
complete independent whole-codebase audit.

### Quant/research perspective

`aitos/learning/evaluation_plan.py` creates deterministic rolling training/test
partitions with a reserved locked holdout. The conservative pre-test gap equals
the declared maximum label horizon plus an additional buffer; a corresponding
buffer is reserved before holdout. Inputs and folds are bounded (at most 200).
Dates use UTC, half-open intervals. A too-short history returns insufficient
history, not a passing zero-trade backtest.

The Research Lab now has a working **Evaluation planner**: date/window/gap fields,
optional dataset fingerprint, calculated folds, visible holdout, export and
invalid-input clearing. The GET endpoint computes metadata only. It reads no
market dataset, runs no backtest, changes no existing validator policy and never
promotes a candidate. A provided dataset SHA is explicitly not independently
verified. Actual event/label purging, trial-count correction, realistic cost/fill
model calibration and empirical out-of-sample validation remain separate gates.

### Financial/trading-logic perspective

`aitos/trading/performance.py` analyzes a bounded sample of recorded closed trades:

- Net recorded PnL, wins/losses/breakeven, win rate, arithmetic expectancy,
  median, best/worst record and profit factor.
- Profit factor remains unavailable without observed negative PnL; no infinity
  or fabricated loss-free edge is shown.
- Cumulative recorded PnL and peak-to-trough closed-PnL drawdown in USD. Trades
  sharing a final-close timestamp are grouped before drawdown is observed.
- Stable float-expansion prefixes preserve low-order PnL across large cancelling
  closes; e.g. `1e16, 1, -1e16` ends at 1, not zero. Derived nonfinite/overflow
  evidence is rejected.
- LONG/SHORT and strategy attribution are descriptive only. Groups are built
  once rather than repeatedly rescanning all records per strategy.
- Fees/funding coverage is explicit. Missing fields remain unavailable and costs
  are not subtracted a second time from net recorded PnL.
- Deterministic SHA-256 over normalized input records; this is not a signed
  source-authenticity or immutable database-snapshot certificate.

No strategy, signal, entry/exit threshold, position sizing, leverage limit or risk
policy was changed. No annualized return, Sharpe, statistical significance,
mark-to-market equity or account performance is inferred from trade PnLs.

### Backend perspective

Authenticated read-only `/console/v1/performance` returns at most 1,000 selected
ledger records' analytics with fixed bounds, no arbitrary caller query, no-store
headers, sanitized failure and a five-second await limit. One bounded query reads
one extra record to disclose truncation; it does not loop over shifting offset
pages. Selection is **latest ledger updates**, not necessarily latest close dates;
updated historical cost corrections can enter the sample. Display is then sorted
by final close time.

`/console/v1/research/plan` is bounded read-only computation. It rejects unknown,
duplicate, malformed and excessive inputs. Attached systems use the same console
bearer authorization. Neither endpoint launches a job or mutates runtime state.

History/performance query-local budgets: 3 seconds engine execution setting,
64 MiB bytes-to-read setting, 128 MiB memory setting, and throw-on-read-overflow.
These are local query guards, not measured production capacity or Redis/host
configuration changes. Startup/full recovery does not reuse the bounded UI path.

### Database perspective

Closed-ledger reads no longer select an arbitrary `argMax` result on tied retained
versions or silently skip corrupt realized PnL. They select latest retained
versions first, count distinct payloads, validate identity/terminal state/finite
PnL and cost fields, and fail closed on invalid evidence. Closed writes reject
invalid PnL and nonfinite JSON before inserting. Runtime JSON writes also reject
NaN/Infinity.

REAL ENGINE REPRODUCTION: running the previous aggregate query in chDB produced
ClickHouse `Code: 184 ... any(payload_json) AS payload_json ... inside another
aggregate function (ILLEGAL_AGGREGATION)`. The alias shadowed the source column
used by the neighboring `uniqExact`. Both runtime and closed-ledger queries now
use a distinct `latest_payload` alias, verified against an actual embedded engine.

Test-only chDB 4.4.0 / chdb-core package 26.9.0 reports ClickHouse **26.9.2.1**.
Tests create isolated tables, stop merges only inside the fixture to retain
conflicts, insert deterministic fixture rows, run actual SQL/parameters/settings
and close the isolated session. This is **not** a network server, production
version, ClickHouse Connect transport or restart/failover certification.

Timestamp ties already discarded by ReplacingMergeTree cannot be reconstructed.
Strict monotonic revisions, multi-writer fencing, account-scoped storage design,
legacy data remediation and deployment-version verification remain open.
No production migration, ALTER/DELETE cleanup, or production database operation
was performed.

### Frontend/design perspective

There are now nine pages, including **Performance**. New features include:

- Provenance-led performance hero, four metric cards and recorded-PnL SVG chart.
- Keyboard-operable observation slider, risk context and drawdown caveats.
- Direction/cost-coverage cards, strategy table, export and evidence fingerprint.
- Explicit loading, empty, unavailable and refresh behavior; no demo performance
  is substituted for an unattached ledger, even in Sample workspace.
- Typed output validation, monotonic curve/window/count checks, financial-summary
  consistency and malformed-cost rejection.
- Research planner with readable partitions, holdout context, parameter bounds,
  error states and JSON export. Editing a field discards the previous result.
- Responsive layouts and scrollable navigation for short viewports.

Cross-language fixtures are checked against the actual Python producers. They
are test inputs, not injected app data. The performance browser screenshot uses
an explicitly watermarked synthetic contract fixture; the running unattached
preview correctly shows performance unavailable.

## Verified evidence

| Check | Observed result |
|---|---|
| Current backend non-integration suite | **1731 passed, 21 deselected, 72.64s** |
| Separate actual embedded ClickHouse checks | **6 passed, 0.86s** |
| Frontend unit/contract tests | **51 passed** |
| TypeScript + production bundle | **Passed** |
| npm audit | **0 vulnerabilities** at verification time |
| Browser smoke | **Nine pages passed**, planner/export/invalid-input clearing, performance unavailable + watermarked fixture, evidence export, runtime/sample isolation, dialogs, mobile overflow and network-failure clearing |
| Python static checks | Black on 9 scoped files, focused Flake8 F, compile checks and diff whitespace passed |
| Installed Python dependencies | `pip check`: no broken requirements |

Logs: `tmp/backend-research-ledger-final.log`,
`tmp/clickhouse-embedded-ledger-final.log`,
`tmp/console/research-performance-build.log`.

The broad run excludes 21 integration cases; six of those were then run separately
and passed. The other service integration cases were not run in this increment.

Browser evidence: `tmp/console/research-planner.png`,
`tmp/console/performance-test-fixture.png`,
`tmp/console/performance-mobile-test-fixture.png`. The performance screenshots
are watermarked TEST FIXTURE, not actual financial outcomes. Browser harness
issues (exact accessible name included nav indices; packaged Chromium single-
process context lifecycle) were fixed and the complete smoke rerun passed.

Optional local analytics microbenchmark: 1,000 deterministic synthetic records,
1,000 strategy groups, 3 warmups, 20 measured runs, nearest-rank percentiles:
p50 **17.01ms**, p95 **23.39ms**, p99/max **72.90ms**. This small-sample local CPU
measurement excludes SQL, transport, exchange, event pipeline and UI. It is not
production trading latency or an SLO. Data:
`tmp/console/analytics-microbenchmark.json`.

## CI, environment and safety

Optional engine requirements are pinned in
`tests/requirements-clickhouse-embedded.txt`, separate from production runtime
requirements. New `.github/workflows/ledger-quality.yml` checks the ledger
contracts and actual embedded SQL on PR/manual execution with contents-read
permission, no credentials and no deployment. Remote workflow execution is
NOT MEASURED; no push or dispatch was performed.

VERIFIED: no live orders, venue/account connection, strategy/risk change, VPS
activation, native installer deployment, production data deletion, branch switch,
commit or push occurred. Existing broad dirty-tree changes were preserved.
Local console remains port 5173, with an unattached adapter on loopback 8765.
The Vite proxy is still not production operator authentication; never attach
real financial data through an unauthenticated public preview.

## Remaining release gates — do not mark complete

1. Strict revision-based persistence/writer ownership and real server restart,
   failure, durability, merge and transport evidence on the deployment version.
2. Current account/open-algo/terminal-child reconciliation and scoped storage.
3. Isolated paper replay/soak with dated end-to-end latency, loss/error windows,
   cost/fill assumptions and reproducible research evidence.
4. Authenticated operator gateway, secure vault, audited account/mutation control.
5. Real equity/market-price/cash-flow lineage and consistent snapshot pagination.
6. Evaluated model-assisted analyst integration, full accessibility/OS review,
   signed native packaging/update and independent security/quant review.

Rollback: disable optional console endpoints by omitting the token and stop local
preview processes if needed. Review recovery-source rollback offline; do not
clear quarantines, delete ledger rows or swallow ambiguity exceptions to bypass
new integrity gates. Preserve journals and source evidence.
