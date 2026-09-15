## AITOS Development Environment — Complete Status

### Git Branch Analysis
| Branch | Status |
|--------|--------|
| `origin/main` (HEAD=08c0276) | 2260+ commits, has `ManagedStreamSet` + `ManagedOrderBookStream` inline in `binance.py` (1026 lines), plus `price_safety_net.py`, `price_freshness.py`, and `test_price_safety_net.py` |
| `origin/fix/reconnect-storm-and-position-safety` | 33 commits ahead of main, has `managed_streams.py` (199 lines) + `connect_raw_dynamic.py` (255 lines) extracted from binance.py |
| `origin/fix/reconnect-storm-position-safety-v2` | 0 commits ahead of main, has `binance.py` (553 lines), no `managed_streams.py` |
| `origin/feat/exit-intelligence-v2` | 1168 commits behind main, same lifecycle as main |

### New Files Created
1. **`aitos/exchange/connect_raw_dynamic.py`** (254 lines, 10,652 bytes) — Extracted `connect_raw_dynamic()` function with incremental SUBSCRIBE/UNSUBSCRIBE logic, borrowed from the reconnect branch
2. **`aitos/exchange/managed_streams.py`** (195 lines, 7,246 bytes) — `ManagedStreamSet` + `ManagedOrderBookStream` classes, matching the reconnect branch implementation
3. **`tests/exchange/test_connect_raw_dynamic.py`** (756 lines, 22 tests) — Full test suite for connect_raw_dynamic (all 22 passing)

### Modified Files
- **`aitos/exchange/binance.py`** (605 lines, down from 1028):
  - Added imports for `connect_raw_dynamic`, `ManagedOrderBookStream`, `ManagedStreamSet`
  - Replaced the 200-line inline `_connect_raw_dynamic` method body with a 6-line delegation to the shared function
  - **Removed duplicate inline `ManagedStreamSet` + `ManagedOrderBookStream` classes** (was lines 611–820) — they were broken/inferior and duplicated from `managed_streams.py`
- **`aitos/eventbus/__init__.py`** — Restored `install_pipeline_stage_telemetry()` with lazy import pattern. Function defined at module level with `from aitos.forensics.pipeline_stage_telemetry import install` inside the function body. Called immediately after definition. Circular import broken: `eventbus → forensics → exchange.binance → connect_raw_dynamic → market_data → eventbus` is now cut by the lazy import inside the function.

### Tests
- **Full suite: 826 passed, 7 failed, 6 warnings** (51.10s)
- 7 failures are all `test_app_wiring.py` — ClickHouse auth failure (`AUTHENTICATION_FAILED`, code 516), unrelated to our changes (ClickHouse password mismatch in env)
- **Exchange + risk + price safety: 40 passed** (connect_raw_dynamic 22 + managed stream 6 + price safety 5 + risk engine 7)
- All new tests pass (22/22 in test_connect_raw_dynamic.py)

### Committed & Pushed
- Commit `1283a2d` pushed to `origin/main`
- 5 files changed, 1377 insertions, 433 deletions

### Remaining Issues
1. **`test_app_wiring.py`** — 7 failures due to ClickHouse auth mismatch (code 516), not caused by our changes
2. **`ENVIRONMENT_STATUS.md`** is committed and pushed

### Environment Status
- 12 AITOS Skills loaded
- 8 MCP servers installed
- Docker infra healthy (Redis, ClickHouse, Neo4j)
- Virtual env active with full + backtest deps