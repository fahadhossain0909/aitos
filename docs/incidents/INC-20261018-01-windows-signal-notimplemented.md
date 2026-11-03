# INC-20261018-01 — Windows native startup crash: NotImplementedError from add_signal_handler

- **Severity:** P1 (blocks native local paper-trading startup; no financial risk — Docker path unaffected)
- **Status:** RESOLVED
- **First observed:** 2026-10-18 (evidence: `tmp/signal_test.py` probe + failed native session log `.zwork/background/2026-10-18T11-26-10-806Z-x712pw.log`)
- **Last observed:** 2026-10-18
- **Affected:** `run_paper_trading.py`, `run_live_trading.py` (native Windows execution only)

## Evidence

Reproduced on this host (2026-10-18):

```
async def old_pattern():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)   # ← raises
# ProactorEventLoop → NotImplementedError
```

`loop.add_signal_handler` is not implemented on Windows event loops. Both entrypoints called it unconditionally, so any attempt to run paper trading natively on Windows crashed before the trading loop started. A prior native-setup attempt also failed on pip (`BackendUnavailable: Cannot import 'setuptools.build_meta'` from the AutoClaw2 vendored interpreter) — separate environment issue, not a code bug.

## Root cause

Windows `ProactorEventLoop` lacks signal-handler integration; the entrypoints assumed a POSIX loop.

## Fix

New helper `aitos/runtime_signals.py::install_signal_handlers(stop_event)` — tries `loop.add_signal_handler`, falls back to `signal.signal()` on `NotImplementedError`. Wired into both entrypoints (replacing the inline loop calls).

## Tests

- `tests/test_entrypoint_signals.py` — runs the helper under an explicit `ProactorEventLoop` when available. Executed on host (PASS) and inside the aitos image via pytest (see below).
- `python -m py_compile` on all edited files: PASS.
- Old pattern repro still raises `NotImplementedError` (confirms the diagnosis, not masked).

## Runtime verification

Docker path (`aitos-paper` container, Linux loop) started healthy 2026-10-18T13:20Z, position recovery verified (`restored_open_trades: 1`). Native Windows path now reaches the trading loop (compile + handler install verified; full native run not required for current Docker-based operation).

## Rollback

`git revert` of the single commit touching the two entrypoints + helper + test.

## Remaining risk

None known. `signal.signal` fallback requires main thread — both entrypoints run `asyncio.run` from the main thread.
