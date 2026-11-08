# Recorded Binance account evidence — implementation and verification

## Executive status

VERIFIED: an **offline, read-only, Binance-shaped recorded-response adapter** now
feeds the account comparator. Added schema 2 for receipt-window and position-mode
binding, exact margin-balance semantics, strict file loading, and a usable CLI.
This is not a Binance connector activation or proof that a real account reconciles.

Branch `arena/ed127d35-aitos`; existing work preserved. No real provider request,
credentials, account access, live orders, deployment, production migration, journal
cutover, strategy/risk/Redis changes, commit/push or remote CI dispatch.

## Existing path inspected

Inspected `exchange/binance.py::fetch_income_history`,
`exchange/cost_adjustments.py` and `execution/position_integrity.py`, plus current
account snapshot contract and tests. The active cost-adjustment service already
holds its cursor on unattributed rows in current source. This tranche does not
change its cursor, publisher, scheduling, lifecycle amendments or reconciliation
authority. The new adapter is opt-in offline evidence conversion, not a duplicate
active fetcher, scheduler or event consumer.

## Delivered contracts

### Recorded bundle to normalized snapshot

`aitos/exchange/binance_account_evidence.py::adapt_recorded_account` accepts exactly:

- `schema_version: 1`, logical `account`, `mode: paper|testnet`;
- integer `server_time_ms`, aware fresh `observed_at`;
- explicit `position_mode: ONE_WAY|HEDGE`;
- inclusive `income_start_ms`, `income_end_ms`, ending no later than server cut,
  within one bounded seven-day window;
- `page_size` 1–1000 and `income_pages` with full preceding pages and a short
  terminal page, at most 101 pages / 10,000 accepted receipts;
- recorded `balances` and `positions`, each bounded to 10,000 rows.

Balance inputs use Binance-shaped `asset`, `balance`, `availableBalance`. Position
inputs use `symbol`, `positionSide`, `positionAmt`. Income inputs use `incomeType`,
`tranId`, `time`, `income`, `asset`. Only whitelisted fields enter normalized output;
raw extra provider fields are not exported.

Supported income kinds: COMMISSION, FUNDING_FEE, REALIZED_PNL, TRANSFER. Unknown
kinds are unavailable, not silently skipped. Receipt identity includes income kind
and exact transaction ID; IDs accept bounded integer/decimal-string representations,
never float/bool. Duplicate identity across pages, including conflicting currency
or amount, blocks the bundle rather than silently deduplicating it. Same tranId
under different income kinds remains distinct. Every receipt time must be inside
the declared inclusive window.

ONE_WAY requires BOTH rows; sign becomes LONG/SHORT. HEDGE requires explicit
consistent side/sign and retains both legs without netting. Flat rows are omitted
only after validation; duplicate raw identities are rejected even when flat.
`Decimal.copy_abs()` preserves all digits independent of ambient Decimal precision.
No float conversion, amount summation, epsilon matching, currency conversion or
fee/funding mutation. Raw provider objects are not modified.

### Normalized schema 2

`trading/account_reconciliation.py` retains schema-1 validation and adds explicit
schema 2 `position_mode` and `receipt_window`. Comparison identity now binds schema,
provider/account/mode, common cut, position mode and exact window. Different
windows/modes/schemas produce unavailable, never matched. Normalized one-way
snapshots cannot contain opposite legs for the same symbol.

Schema 2 compares **signed** wallet/available values exactly. Available margin can
exceed wallet balance, and negative balances/shortfall remain observable data;
they are not coerced to zero or interpreted as trading readiness. Schema 1 keeps
its earlier nonnegative `available <= total` contract unchanged. A match of two
negative accounts is still only equality, not solvency/risk authorization.

### Strict offline CLI

```sh
python -m aitos.exchange.binance_account_evidence expected-schema2.json recorded-bundle.json
```

Both reconciliation commands now share a 4 MiB bounded JSON loader rejecting
duplicate fields at every object level and NaN/Infinity constants. Malformed inputs
print only sanitized unavailable output, not file contents/account/receipt details.
Exit codes: `0` matched, `2` mismatch, `3` unavailable. Output includes no money,
account IDs or raw errors; both authority/certification flags remain false.

`expected-schema2.json` must be independently prepared. Deriving expected values
from the same captured response is only a synthetic pipeline test, not independent
reconciliation evidence. Files need current timestamps; historical fixtures must
not be presented as fresh runtime observations.

## Verification

| Check | Actual result |
|---|---|
| Full backend non-integration | **2045 passed, 37 deselected; 71.26s** |
| Adapter + comparator + existing income reconciliation | **76 passed; 0.18s**, included above |
| New provider-boundary regressions | **41 cases**, included above |
| Actual CLI subprocess matched/mismatch/unavailable | PASS with synthetic inputs; exit codes 0/2/3 |
| Frontend | **84 passed**, production build PASS; source unchanged |
| Focused Black/F-lint/Bandit/compile/whitespace | PASS; no new suppression |
| Persistence workflow YAML | Syntax PASS; remote execution NOT TESTED |
| Embedded/browser/Windows/service suites | Not rerun this increment |

Logs: `tmp/account-provider-final/{backend,frontend,build}.log` and
`cli-verification.json`. CLI files under that directory are explicitly synthetic.
No real account, balance, credential or signing key was used.

Tests include hedged offsetting legs, tiny exact differences, long decimal under
precision 6, signed margin balances, wrong scope/window/schema, unsupported income,
wrong signs/modes, zero/duplicate positions, duplicate receipts/pages, absent terminal
page, stale/invalid metadata, float IDs/money, hidden duplicate JSON and sanitized
CLI errors. Existing income reconciliation tests remain passing.

## Limits and remaining gates

Structural page validation **cannot prove** that a provider or capture process did
not omit intermediate pages/rows, or that the complete balances/position response
was captured. No provider signature/authentication, request/response provenance or
atomic exchange snapshot is established. A supplied account label/cut is caller
metadata, not exchange-verified identity. Concurrent account changes may require
trusted bracketing/reconciliation before equality can be relied on.

This adapter supports a bounded explicit subset, not every Binance account mode,
income type, portfolio/multi-asset margin schema or historical backfill interval.
Unsupported records fail closed. No active runner/financial authority consumes
these reports. Receipt window/position-mode requirements intentionally prevent
schema-1 values being silently promoted to schema-2 provider evidence.

Real provider-account verification, remote service soak/HA, Windows build/signing,
independent review and full recovery authority cutover remain **NOT TESTED / pending**.
Representative latency/capacity p50/p95/p99/max: **NOT MEASURED**. No production or
continuous-monitoring certification is claimed.

## Rollback / next step

Omit this explicit offline adapter to retain the earlier path; no service or DB
migration requires rollback. Schema 1 remains compatible. Preserve evidence files
and all unrelated working-tree changes; do not reset data or Git. Add this code
and window/mode/decimal semantics to the independent review bundle. Next external
gate is reviewed provider capture and controlled service/Windows validation, not
activation of live credentials or automatic financial repair.
