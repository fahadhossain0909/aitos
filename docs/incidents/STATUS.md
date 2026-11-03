# AITOS Incident Status — quick operational summary

Updated: 2026-10-19T22:12Z | Branch: `fix/market-data-publish-and-flow` | Mode: paper (Docker)
## Open incidents

| ID | Severity | Status | Summary |
|---|---|---|---|
| [INC-20261018-02](INC-20261018-02-clickhouse-single-row-write-latency.md) | P2-low | INVESTIGATING | ClickHouse single-row book-snapshot writes 100-300ms; no backlog/loss; remediation options prepared, needs approval |

## Resolved

| ID | Severity | Summary |
|---|---|---|
| [INC-20261018-01](INC-20261018-01-windows-signal-notimplemented.md) | P1 | Windows `add_signal_handler` NotImplementedError crashed native startup; fixed via `aitos.runtime_signals` helper + regression test |
| [INC-20261018-03](INC-20261018-03-docker-desktop-down-stack-recovery.md) | P1 | Docker Desktop force-closed while host up → whole stack down ~3h (containers Exited 137, no auto-restart); recovered via Docker Desktop start + compose up; 2 positions restored cleanly; host-level watchdog proposed for true 24/7 |
| [INC-20261019-01](INC-20261019-01-gateway-drain-unbound-event.md) | P2 | `drain_once` idle timeout could raise `UnboundLocalError`; initialized `event = None`, added regression test, runtime verified |
| [INC-20261019-02](INC-20261019-02-a-stat-side-blind-and-duplicate-history.md) | P2 | A-Stat funding/OI side-blind conversion, duplicate history accumulation, and missing finiteness guards; runtime verified |## Known non-incidents / signatures (do not re-derive)

- **Stack fully down + all containers `Exited (137)` + Docker Desktop process absent + host uptime predates last healthy check** → Docker Desktop closed/crashed (NOT a PC reboot). Recovery: start `Docker Desktop.exe`, wait for engine, `docker compose --profile observability up -d`, then verify `restored_open_trades` in aitos-paper logs. See INC-20261018-03.
- pip `BackendUnavailable: Cannot import 'setuptools.build_meta'` → AutoClaw2 vendored interpreter; use system Python or Docker.
- `position.monitor.stale_candidate` + `rest_fallback_requested` + `stale REST trade response filtered` for a restored position symbol right after startup → transient: REST fallback re-subscribing market data for a symbol before WS catches up; self-resolves.
- Gateway drain-worker shutdown hang → already fixed in working tree (idle-timeout `drain_once`), uncommitted on this branch as of 2026-10-18.

## Monitoring

- Tier-1 script: `scripts/runtime_monitor.py` (stdlib-only, always exit 0) → `tmp/monitor/report.json`, `history.log`, `alerts.json` (non-empty ⇒ Tier-2 escalation trigger).
- Scheduled automation: every 15 minutes (CronCreate, this workspace).
