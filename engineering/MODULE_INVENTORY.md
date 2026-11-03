# AITOS Module Inventory

## Status legend
- DOCUMENTED: stated by current README or existing repository skill; not proof of runtime operation.
- SOURCE-VERIFIED: implementation and call paths inspected.
- RUNTIME-VERIFIED: observed working in a named environment with evidence.
- PARTIAL / DISCONNECTED / BROKEN: use only with evidence.
- NOT INSPECTED: not yet audited.

## Initial inventory
| Subsystem | Known repository surface | Current status | Required verification |
|---|---|---|---|
| Paper runner | `run_paper_trading.py` | DOCUMENTED | Startup, dependencies, actual event consumption, paper fills, health |
| Live runner | `run_live_trading.py` | DOCUMENTED; safety-critical | Static review only unless explicitly authorized; never submit live orders |
| Continual learning | `run_continual_learning.py` | DOCUMENTED | Worker lifecycle, input/output, resource limits |
| Exchange adapter | Binance USDT-M REST/WebSocket | DOCUMENTED | Endpoint, subscriptions, event freshness, reconnect/fallback |
| Event transport | Redis Streams / Event Bus | DOCUMENTED | Groups, offsets, pending, ACK, DLQ, lag, throughput |
| Historical persistence | ClickHouse | DOCUMENTED | Inserts, schema, query latency, retention, source-of-truth semantics |
| Knowledge graph | Optional Neo4j | DOCUMENTED | Activation gates, callers, failure isolation |
| Opportunity scanner | Scanner / candidate intelligence | DOCUMENTED | Candidate throughput, rotation, ranking, stale state |
| Risk controls | Risk engine / circuit breaker / exposure | DOCUMENTED | Exact call sites and paper/live separation |
| Trade lifecycle | Position/order lifecycle | DOCUMENTED | Persistence, partial exits, close confirmation, restart recovery |
| Backtesting | `aitos.backtest.cli`, `rich_cli` | DOCUMENTED | Test coverage and parity with live decision contracts |
| Health/metrics | `aitos.health_server`; `/health`, `/metrics` documented | DOCUMENTED | Actual bind address, module readiness semantics, scrape |
| Monitoring utility | `monitor_aitos.py` | SOURCE SURFACE FOUND | Inspect all checks for assumptions, false positives, side effects |
| Agent instructions | `skills/aitos-skill.md`, `skills/aitos-prompt.md` | SOURCE FOUND | Reconcile with AGENTS.md; skill is mandatory operational reference |

## Inventory update template
For each concrete module add:
- Import path and owning package.
- Entrypoint/initializer and shutdown hook.
- Direct dependencies and event contracts.
- Configuration keys and defaults.
- State read/written and persistence owner.
- Consumers/callers and downstream effects.
- Tests (unit/integration/contract).
- Metrics/logs/traces.
- Paper/live impact.
- Startup/readiness and restart behavior.
- Evidence links, test command, runtime window.
- Status and unresolved questions.

Do not infer a module's health from its name, file existence, class implementation, or a successful process start.
