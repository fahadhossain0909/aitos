# Live Pending-Order Risk Snapshot Provider

## Contract

`LivePortfolioTracker` accepts an optional async `pending_order_snapshot_provider`. It must return one coherent mapping containing:

- `orders`: the complete authenticated open-order inventory for the account and relevant market
- `mark_prices`: fresh symbol-to-mark-price mapping
- `stop_prices`: validated `(symbol, LONG|SHORT)` to protective thesis-stop mapping
- `leverage_by_symbol`: current symbol leverage mapping
- `orders_observed_at` and `risk_inputs_observed_at`: timezone-aware observation timestamps
- optional `now`: timezone-aware evaluation clock for deterministic replay/tests

The tracker passes this evidence through `pending_exposures_from_open_orders`. The adapter validates freshness/skew, identity uniqueness, order status/type, remaining quantity, reduce-only/hedge closing semantics, mark/stop direction, and finite risk values. It projects only remaining unfilled entry quantity into portfolio gross exposure and stop-risk.

## Fail-closed behavior

When a provider is configured, every `refresh_equity()` invalidates the previous snapshot before awaiting new evidence. A provider error, missing field, malformed order, stale timestamp, missing mark/stop/leverage, or invalid stop prevents refresh from completing. `build_portfolio_state()` also refuses to build a live risk snapshot until the latest pending-order evidence has been validated.

No provider is configured by default, preserving existing paper and test harness behavior. This is an explicit integration seam, not a claim that a venue adapter has already been wired to the authenticated account. The live entrypoint separately calls `require_live_account_exposure_reconciled()` before every fresh-exposure scan; any ordinary pending entry order without a validated stop-risk projection blocks the next scan. That strict gate remains active until a provider can join each pending entry to its own durable thesis-stop provenance. Do not borrow a stop from another trade or infer one from symbol/side alone. A production provider must source a complete authenticated exchange inventory and point-in-time mark/stop/leverage data; partial endpoint responses are not sufficient.

## Verification

Tests cover a partially filled entry order (only remaining quantity is projected), resulting notional/risk/leverage, stale snapshot rejection, and refusal to use the tracker after failed refresh. No order is submitted, amended, cancelled, or otherwise mutated.
