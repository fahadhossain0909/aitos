# INC-20261018-03 — Paper stack down: Docker Desktop closed/crashed while host stayed up

- **Severity:** P1 (full paper-trading + monitoring outage; no financial risk — paper mode, positions persisted safely)
- **Status:** RESOLVED (service restored 2026-10-18T17:41Z; supervision gap remains — see remaining risks)
- **First observed:** 2026-10-18T17:38Z (monitor cycle `CRITICAL: docker unavailable, health endpoint unreachable`)
- **Outage window:** between 13:52Z (last HEALTHY cycle) and 17:38Z (~≤3.75h); all containers show `Exited (137)` "3 hours ago" → engine was force-killed ~14:4xZ
- **Affected:** entire local stack (all 11 containers), monitoring continuity (ZCode automations also paused while the app was closed — queued runs arrived in a batch)

## Evidence

- `docker ps -a`: every service `Exited (137)` ≈3h before diagnosis; `clickhouse-init` cleanly `Exited (0)`.
- Docker engine: `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`; `Docker Desktop.exe` process absent.
- Host NOT rebooted: `LastBootUpTime` 2026-10-18 21:28 local (13:28Z) — boot predates the healthy 13:52Z cycle.
- Health endpoint: connection refused (000).

## Root cause

Docker Desktop (engine + app) terminated ungracefully while the host kept running — most likely closed or crashed during the user session gap. `restart: unless-stopped` did NOT bring containers back when the engine returned (hard-killed engine leaves them exited), so the stack stayed down until manual recovery.

## Recovery actions (17:39–17:41Z)

1. Started `Docker Desktop.exe` (per-user install path), polled `docker info` → engine 29.8.1 up.
2. `docker compose --profile observability up -d` → all 11 containers back.
3. `aitos-paper` healthy; recovery verified: `restored_open_trades: 2` (ARKMUSDT, ARMUSDT), market-data reconciliation 2/2 verified, 0 failures. (Earlier ACNUSDT position closed normally before the outage — only open trades are restored.)
4. Monitor cycle 17:41Z: `HEALTHY docker=11 health_reachable=True` (1 ERROR line in bounded tail = startup noise from the kill, non-persistence).

## Remaining risks / follow-up

- **Supervision gap:** recovery only happened when ZCode reopened and the queued automation fired. If Docker Desktop dies while the app is closed, the stack stays down silently. Follow-up (needs user decision): a host-level watchdog (Windows Task Scheduler check → start Docker Desktop + `compose up`) for true 24/7.
- Root cause of Docker Desktop termination (user-initiated close vs crash) unknown — no Docker Desktop logs inspected yet; not worth deep-diving unless it recurs.

## Rollback

N/A (service restoration only; no code/config changes).
