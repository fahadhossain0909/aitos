---
name: aitos-operations
description: Local operations skill for running, monitoring, and safely remediating AITOS paper trading on the Windows host. Use when starting/stopping the paper-trading stack, checking health/telemetry/positions, diagnosing runtime incidents, or deciding what changes are authorized. Complements (does not replace) skills/aitos-code-intelligence/SKILL.md for deep code forensics.
---

# AITOS Local Operations Skill

Repository: https://github.com/fahadhossain0909/aitos — local checkout `C:\aitos`.

Mission order (never invert):
1. Prevent unintended live trading / financial exposure.
2. Preserve paper-trading continuity, especially open-position monitoring across restarts.
3. Keep market data fresh and flowing end to end.
4. Root-cause genuine failures; never paper over symptoms.
5. Evidence-based, minimal, reversible fixes with regression tests.

## 1. Environment map (verified 2026-10-18)

- Host: Windows 11, Git Bash. Native Python 3.13 (no pytest installed natively — run tests inside the Docker image or with stdlib scenarios). NEVER use the AutoClaw2 vendored Python for pip — its pip lacks a build backend (`BackendUnavailable: Cannot import 'setuptools.build_meta'`).
- Runtime: Docker Desktop. Full stack defined in `docker-compose.yml` (+ `docker-compose.override.yml`).
- Services: redis, redis-dlq-retention, redis-stream-archive, clickhouse (+ one-shot clickhouse-init), neo4j, aitos-paper (health :8090), aitos-learning, aitos-storage-maintenance. Profiles: `backtest` (aitos-backtest), `live` (aitos-live :8091), `observability` (prometheus, grafana).
- **Never start `--profile live` and never run `aitos-live` or `run_live_trading.py` without explicit human authorization.**
- The paper container mounts the working tree read-only at `/app` (`AITOS_SOURCE_ROOT`), so uncommitted local fixes run without rebuilding the image.
- Data: `data/eventbus/redis` (Redis AOF, bind mount), ClickHouse in named volume `aitos_clickhouse-data` (created 2026-10-16; pre-migration backup: `clickhouse-backup.tar.gz` at repo root), Neo4j bind mount `data/databases/neo4j`, models `data/runtime/models`.
- Trade state: ClickHouse (via `aitos.trading.persistent_state.DurableTradingStateStore`); recovery path is `TradeStatePersistence.restore()` + `resubscribe_restored_positions()` in `run_paper_trading.py`.
- Health: `GET http://127.0.0.1:8090/health` (paper). Telemetry module statuses included in payload.

## 2. Operating procedures

Start paper stack (never touches live):
```
docker compose --profile observability up -d
```
Verify: containers healthy → `curl http://127.0.0.1:8090/health` → `docker compose logs aitos-paper --tail 100` shows "paper lifecycle recovery initialized" with `restored_open_trades`.

Stop: `docker compose --profile observability stop` (keep volumes; `down` only if volumes are preserved).

Logs: `docker compose logs -f aitos-paper`. Structured errors carry `aitos_extra` JSON. Persistence errors are the highest-priority log class.

Guardrails:
- No restarts while a scan cycle is mid-flight unless data proves a hang (check health staleness first).
- Restart ≠ fix. Diagnose first, restart only to restore service after evidence.
- Never delete volumes, `data/` state, or ClickHouse tables to "clean up".
- Tests inside image: `docker compose run --rm --no-deps aitos-paper python3 -m pytest tests/<file> -q` (image has full deps).

## 3. Monitoring loop (three tiers — keep them separate)

Tier 1 — Runtime Monitor (cheap, scheduled, no LLM): every few minutes check container status/healthchecks, `/health` payload, ERROR-line delta in aitos-paper logs, data freshness (transport last-event timestamps in health payload), open/closed trade counts, Redis stream lag, disk. Write append-only `monitoring.log` + rotate `monitoring_report.json`. Script: `scripts/runtime_monitor.py`.

Tier 2 — AI Engineering Agent (me + subagents, on anomaly): only invoked when Tier 1 flags an anomaly or a critical incident appears. Investigates logs+code, classifies per autonomy levels, writes regression test, applies only authorized fixes, records incident.

Tier 3 — Journal & Supervisor: append-only incident records in `docs/incidents/` (one file per incident, `INC-YYYYMMDD-NN-slug.md`) plus `docs/incidents/STATUS.md` quick summary. Dedup recurring incidents against this catalog. If the monitoring loop itself dies, a scheduled automation restarts it; monitor script must be idempotent and self-healing (never exit non-zero on its own transient failures; always leave a report).

Poll discipline: bounded log windows (tail N), incremental since-last checkpoints, dedup by signature, cooldowns on repeated alerts, no full-database scans, no aggressive REST polling.

## 4. Autonomy levels (from the operating contract)

- L0 Observe (logs/metrics/code reads): always allowed.
- L1 Isolated tests/diagnostics: allowed when non-disruptive.
- L2 Low-risk reversible fixes (null checks, parsing, telemetry, signal handling): allowed on a fix branch with tests + evidence. NEVER on main.
- L3 Runtime-affecting (Redis consumers, subscriptions, persistence, recovery, concurrency, event schemas): prepare evidence + patch + tests + rollback; get human approval before activation.
- L4 Financially consequential (strategy semantics, leverage/risk limits, live orders, closing live positions, deleting trading state): never autonomous.

Known already-authorized WIP on branch `fix/market-data-publish-and-flow` (previous session, uncommitted): gateway drain idle-timeout + publish-error logging (`market_data/gateway.py`), drain workers 4→8 (`market_data/runtime.py`), CVD threshold retune (`intelligence/scanner.py` — strategy-adjacent; do not extend without approval), swing-window fix (`intelligence/indicators.py`), ClickHouse named volume + `/app` source mount (`docker-compose.yml`).

## 5. Git safety

- Never commit to `main`; never force-push; never delete branches; never rebase shared history.
- Commit only on `fix/*` branches, only changes you authored; leave the user's pre-existing uncommitted work uncommitted unless explicitly told otherwise.
- Do not resume paused VPS/deploy workflows; do not edit GitHub Actions or secrets.

## 6. Known failure signatures (do not re-derive)

- `NotImplementedError` from `loop.add_signal_handler` on Windows — fixed via `aitos.runtime_signals.install_signal_handlers` (2026-10-18, INC-20261018-01). If seen again, check both entrypoints still use the helper.
- pip `BackendUnavailable: Cannot import 'setuptools.build_meta'` — wrong interpreter (AutoClaw2 vendored Python). Use Docker or system Python only.
- Gateway drain worker hang on idle queue at shutdown — fixed by idle-timeout `drain_once` (gateway.py WIP). Symptom was shutdown hang / worker stuck in `queue.get()`.
- ClickHouse rename errors / orphaned tmp dirs — check `clickhouse` container logs; the storage-maintenance service reports `rename_errors`.
- Position invisible after restart — check `restored_open_trades` count at startup, then `resubscribe_restored_positions()` effect; never assume absence of runtime object = position closed.

## 7. Incident report standard

Each incident file: ID, severity (P0 safety / P1 operational / P2 degraded / P3 minor), first+last seen, evidence (log lines, timestamps, telemetry), confirmed facts vs hypotheses, root cause or explicit gap, affected components/modes, actions, files changed, tests executed with results, runtime verification, rollback, remaining risks, status (INVESTIGATING / MITIGATED / FIX IMPLEMENTED — NOT VERIFIED / RESOLVED / BLOCKED).

An incident is RESOLVED only with root cause evidenced, fix tested, runtime verified in a bounded observation window, and the record updated. Use the aitos-code-intelligence skill's output standard for deep investigations.
