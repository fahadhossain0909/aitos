# AITOS Incident Status — quick operational summary

Updated: 2026-10-18T13:30Z | Branch: `fix/market-data-publish-and-flow` | Mode: paper (Docker)

## Open incidents

| ID | Severity | Status | Summary |
|---|---|---|---|
| [INC-20261018-02](INC-20261018-02-clickhouse-single-row-write-latency.md) | P2-low | INVESTIGATING | ClickHouse single-row book-snapshot writes 100–300ms; no backlog/loss; remediation options prepared, needs approval |

## Resolved

| ID | Severity | Summary |
|---|---|---|
| [INC-20261018-01](INC-20261018-01-windows-signal-notimplemented.md) | P1 | Windows `add_signal_handler` NotImplementedError crashed native startup; fixed via `aitos.runtime_signals` helper + regression test |

## Known non-incidents / signatures (do not re-derive)

- pip `BackendUnavailable: Cannot import 'setuptools.build_meta'` → AutoClaw2 vendored interpreter; use system Python or Docker.
- `position.monitor.stale_candidate` + `rest_fallback_requested` + `stale REST trade response filtered` for a restored position symbol right after startup → transient: REST fallback re-subscribing market data for a symbol before WS catches up; self-resolves (observed 13:21:04–05Z, gone after).
- Gateway drain-worker shutdown hang → already fixed in working tree (idle-timeout `drain_once`), uncommitted on this branch as of 2026-10-18.

## Monitoring

- Tier-1 script: `scripts/runtime_monitor.py` (stdlib-only, always exit 0) → `tmp/monitor/report.json`, `history.log`, `alerts.json` (non-empty ⇒ Tier-2 escalation trigger).
- Scheduled automation: every 15 minutes (CronCreate, this workspace).
