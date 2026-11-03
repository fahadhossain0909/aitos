# INC-20261019-01 — Gateway drain_once unbound event on empty queue

- Severity: P2 (degraded reliability; latent shutdown/idle path defect)
- First observed: 2026-10-19 (code review of WIP in `fix/market-data-publish-and-flow`)
- Status: RESOLVED

## Summary

The WIP change to `MarketDataGateway.drain_once()` converted `self.queue.get()` to
`asyncio.wait_for(self.queue.get(), timeout=0.1)` so idle workers return promptly.
However, `event` was not initialized before the `try` block. On timeout, the
`finally` block evaluates `if event is not None:` while `event` is still unbound,
raising `UnboundLocalError` instead of the intended clean `False` return.

## Evidence

- File: `aitos/market_data/gateway.py`
- Function: `MarketDataGateway.drain_once`
- Before fix:
  - `event = await asyncio.wait_for(self.queue.get(), timeout=0.1)` inside `try`
  - `finally: if event is not None: ...`
- On `asyncio.TimeoutError`, Python never assigns `event`, so the finally block
  references an unbound local variable.

## Root cause

Missing initialization of `event = None` before the `try` block. This is a
local variable lifetime defect introduced by the idle-timeout WIP, not a
trading-semantics issue.

## Fix

Initialized `event = None` before `try`. No other behavior changed.

## Tests

- `tests/market_data/test_gateway.py::test_gateway_drain_once_returns_false_on_empty_queue`
- Full gateway suite: 6 passed (Docker image, Python 3.12).

## Runtime verification

- Paper stack healthy.
- 21/21 modules healthy.
- 3 open positions monitored.
- Tier-1 monitor status: HEALTHY.
- Data-flow endpoint healthy: normalize 21203/21203, redis_publish 42406/42406, redis_consumer 19349/19349, errors 0, drops 0.
- Bounded runtime observation (~1+ minute) showed no `UnboundLocalError` or `gateway publish failed` entries.
## Rollback

Revert the single-line initialization in `aitos/market_data/gateway.py`.

## Remaining risk

The fix is unit-tested but has not yet been observed under a long idle drain
window in production-like runtime.