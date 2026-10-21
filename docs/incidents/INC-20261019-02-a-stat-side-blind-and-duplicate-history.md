# INC-20261019-02 — A-Stat bridge side-blind funding/OI and duplicate history

- Severity: P2 (degraded model quality; no safety impact)
- Status: RESOLVED
## Summary

The A-Stat scanner bridge converted funding and open-interest scores without
accounting for the proposed trade side. It also re-fed the full kline-derived
return window on every scan, causing the per-symbol statistical stack to
accumulate duplicate history and inflate confidence.

## Root cause

1. `funding` used a fixed `- _signed(funding_score)` and `open_interest` used
   `_signed(oi_score)`, even though both scores are side-fitness values. For
   `TradeSide.SHORT`, the market-directional conversion was inverted.
2. `features["returns"]` was set to the full `returns` tuple on every call.
   `ContractStatisticalStack.evaluate` appends any provided returns, so repeated
   scans duplicated the overlapping window.
3. No finiteness guard on closes/returns.
4. `MIN_KLINES_FOR_STACK` counted klines rather than usable returns.

## Fix

- Convert funding and open interest side-awarely:
  - LONG: funding `-signed`, OI `+signed`
  - SHORT: funding `+signed`, OI `-signed`
- Seed statistical history once, then append only new returns.
- Add finiteness guards.
- Require at least `MIN_KLINES_FOR_STACK` usable returns.
- Run A-Stat evaluation via `asyncio.to_thread` to avoid blocking the event loop.
- Update stale scanner threshold test to the authorized 6.5 threshold.

## Tests

- `tests/test_scanner.py`: 10 passed
- `tests/test_a_stat_bridge.py`: 3 passed
- `tests/market_data/test_gateway.py`: 6 passed

## Runtime verification

- Paper stack restarted successfully.
- 4 open positions restored and monitored.
- Tier-1 monitor: HEALTHY.
- Long-window verification pending.