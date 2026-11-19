# AITOS Binance Spot Integration

## Scope and rollout status

Spot is a separate market domain that reuses AITOS market intelligence and the
shared capital-protection boundary. It does not reuse the Futures order adapter,
leverage, funding, liquidation or reduce-only assumptions. Options remain a
market-identity extension point only; no Options strategy or execution exists.

Capital authorization currently uses the USD numeraire, so the first Spot rollout
accepts USDT-quoted instruments only. USDC, BTC-quoted and other quote assets
must wait until point-in-time FX conversion is included in portfolio equity,
risk and execution-cost accounting.

The Spot runtime is opt-in. Defaults are:

- `AITOS_SPOT_ENABLED=false`
- `AITOS_SPOT_TESTNET=true`
- `AITOS_SPOT_EXECUTION_ENABLED=false`
- `AITOS_SPOT_ALLOW_MAINNET=false`

The Futures live runner does not construct or start Spot components.

## Architecture

### Market data

`BinanceSpotCanonicalMarketDataAdapter` implements the canonical AITOS
`MarketEvent` envelope and emits canonical trades, exchange-timestamped 24-hour ticker updates, closed
klines, order-book snapshots and depth deltas. Every event carries `market_type=spot` and a distinct
`binance:spot:SYMBOL` instrument identity. Spot events publish on isolated
`market.spot.*` channels; existing Futures consumers remain subscribed to
`market.*` and cannot accidentally mix same-symbol Spot data into Futures
features. A Spot consumer subscribes through
`MarketDataBus.subscribe(..., market_type="spot")`.

Trade, ticker, kline and depth symbols are multiplexed over four bounded
combined-stream connections. The order-book path opens the depth WebSocket and
buffers its first frame before fetching `/api/v3/depth`, bridges buffered diffs
to `lastUpdateId`, checks subsequent `pu`/update continuity, and raises
`SpotOrderBookGap` on a gap. The runtime supervisor restarts that stream and
rebootstraps snapshots; a consumer never continues with a silently stale book.

The runtime exposes explicit publishers into the existing `MarketDataBus`,
including isolated `market.spot.*` channels. It does not start
subscriptions by importing the package. The runtime's
`submit_opportunity()` port accepts the shared Strategy `Opportunity` contract
and routes it through the Spot coordinator and shared CapitalGateway.

### Discovery and intelligence

The REST adapter discovers only instruments which are TRADING, explicitly
Spot-enabled, and quoted in the configured quote asset. It ranks the initial
universe by 24-hour quote turnover and applies a hard symbol limit. The
`SpotOpportunityScanner` is a deterministic liquidity/spread/volatility/flow
pre-filter; it is not a strategy signal and cannot submit orders.

Existing AITOS strategy intelligence remains the signal authority. Futures-only
evidence (funding, open interest and liquidations) is absent for Spot rather
than fabricated as zero. The `SpotTradingCoordinator` calls the shared
`CapitalGateway` with a namespaced instrument key, zero funding cost and
Spot-specific fee/slippage assumptions. Spot reservations therefore do not
collide with same-symbol Futures reservations.

### Execution and inventory

The Binance Spot REST adapter supports exchangeInfo, klines, depth, 24-hour
ticker, account balances, order place/query/cancel, trade-history reconciliation,
and private User Data Stream listen-key lifecycle. Exchange filters use
`Decimal` arithmetic and validate price tick, lot size, market lot size and
notional limits. API credentials are separate Spot-specific `SecretStr`
settings; do not reuse Futures keys unless they are intentionally provisioned
with the exact required permissions. Disable withdrawals.

The coordinator allows long-only market BUY entries bounded by `quoteOrderQty`
and the shared capital allocation. It pins capital before submission. If a
network error makes order acceptance ambiguous, the reservation remains pinned;
query the exchange by client order ID and reconcile fills before releasing it.
Spot SELL is not exchange reduce-only: the exit port requires the shared
`ExitDecision` contract from Exit Intelligence, accepts only `EXIT` or
`MANAGE`, enforces the suggested reduction fraction for `MANAGE`, and verifies
AITOS-managed inventory before sending.
External wallet holdings are never adopted as strategy-owned inventory.

The SQLite inventory ledger is durable, fill-idempotent and records commission
asset. Base-asset and quote-asset commissions are reflected in quantity/cost
basis. Third-asset fees remain explicitly pending until a caller supplies their
point-in-time quote-currency valuation through
`resolve_commission_conversion()`; the ledger never substitutes a current
price for a historical fee. Pending conversions are queryable and survive
process restart. Reconciliation uses exchange trade IDs as idempotency keys. Client order IDs and order intents are journaled before exchange
submission. Ambiguous submissions remain unresolved in that journal across
process restarts. The default Spot runtime also uses a durable capital
reservation database with a stable owner identity so pinned exposure can be
reconciled after restart. REST calls share a weighted token-bucket limiter and
signed requests support explicit server-time offset synchronization.

## Enabling read-only Spot market data

Set:

```dotenv
AITOS_SPOT_ENABLED=true
AITOS_SPOT_TESTNET=true
AITOS_SPOT_EXECUTION_ENABLED=false
AITOS_SPOT_ALLOW_MAINNET=false
AITOS_SPOT_QUOTE_ASSET=USDT
AITOS_SPOT_MAX_SYMBOLS=20
```

Construct the runtime explicitly with `build_spot_runtime()`, then call
`await runtime.start_market_data(market_bus)`. It discovers the highest-turnover
eligible USDT Spot symbols and starts supervised trade/ticker subscriptions
for the bounded universe plus book/kline subscriptions for the top five.
Call `await runtime.stop_market_data()` before shutdown, or
`await runtime.close()` to stop streams and close durable stores. The Futures
runner does not call this factory automatically.

## Futures↔Spot lead-lag intelligence

The Spot runtime also starts a read-only `FuturesSpotLeadLagAnalyzer`. It
subscribes to the existing USD-M Futures ticker channel and the isolated Spot
ticker channel, maps the same underlying symbol, and aligns exchange event
timestamps into one-second buckets. Spot uses Binance's exchange-timestamped
24-hour ticker stream (best bid/ask plus last price), rather than receipt-time
bookTicker timestamps, so lead-lag estimates are not based on local arrival
order.

The analyzer computes log-return correlations in both directions over a
rolling 15-minute window and tests lead horizons from zero to five seconds.
It reports the leading market, lag, signed correlation, 95% Fisher confidence
interval, and aligned observation count. It explicitly reports insufficient
data, low confidence, ambiguous leadership, or no material lead rather than
forcing a direction. Symbol aliases can be supplied to `build_spot_runtime(futures_spot_symbol_aliases=...)`; no guessed
contract-to-spot mapping is applied.

These values are attached to ranked Spot pre-filter candidates as
`futures_spot_lead_lag_*` diagnostics. They do **not** change the scanner
score, create a trade signal, override the shared Strategy/HTF/CVD/flow gates,
or authorize an order. A Futures lead can be a price-discovery confirmation;
a Spot lead can indicate Spot-originated flow; disagreement or unstable
correlation is a caution/uncertainty signal. Strategy weighting must wait for historical replay, fees/slippage-aware
execution tests, autocorrelation-aware confidence calibration, and
symbol/regime out-of-sample validation. The analyzer is observational telemetry
until those gates pass.

## Testnet execution

Only after unit/CI checks pass, configure a dedicated Binance Spot Testnet API
key and secret. Then explicitly enable `AITOS_SPOT_EXECUTION_ENABLED=true`.
The Spot executor still defaults to disabled in direct construction. Testnet
does not model real liquidity, fees, slippage or market impact and cannot prove
profitability.

## Mainnet controls

Mainnet market data may be selected by setting `AITOS_SPOT_TESTNET=false`.
Mainnet order placement additionally requires all of:

1. `AITOS_SPOT_ENABLED=true`
2. `AITOS_SPOT_EXECUTION_ENABLED=true`
3. `AITOS_SPOT_TESTNET=false`
4. `AITOS_SPOT_ALLOW_MAINNET=true`
5. The runtime factory's explicit `mainnet_approved=True` launch argument
6. Dedicated Spot API credentials with withdrawals disabled

No deployment should set these values until Testnet order lifecycle,
restart recovery, inventory reconciliation, capital reservation release,
fees and execution-cost replay have passed operator review.

## Remaining production gates

- Bind the existing strategy scanner's Spot candidate flow into the Spot
  coordinator and existing lifecycle's entry/exit callbacks; current scanner
  is a pre-filter only.
- Validate end-to-end MarketDataBus consumers and all Python 3.10-3.12 CI jobs.
- Add exchange-specific rate-limit budgeting and server-time offset monitoring.
- Test live User Data Stream keepalive/reconnect and account/order event recovery.
- Add order lifecycle integration tests against Spot Testnet, including
  partial fills, cancel/replace, ambiguous POST recovery and process restart.
- Convert third-asset commissions to quote currency using point-in-time prices;
  do not report final net PnL while conversion is pending.
- Extend execution replay with Spot maker/taker fees, spread crossing,
  partial fills, market impact and latency; validate by symbol and regime OOS.
- Wire a point-in-time third-asset fee-price source into the pending fee-conversion
  ledger; until resolved, net PnL remains provisional for affected fills.
- Extend execution replay with Spot maker/taker fees, spread crossing,
  partial fills, market impact and latency; validate by symbol and regime OOS.
- Verify the existing portfolio mark-to-market aggregates Spot inventory,
  quote cash and Futures margin without double-counting shared quote assets.

Options are intentionally not implemented. `MarketType.OPTIONS` remains
available for a future dedicated options design and strategy package.
