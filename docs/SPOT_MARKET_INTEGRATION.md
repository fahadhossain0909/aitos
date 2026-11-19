# AITOS Binance Spot Integration

## Scope and rollout status

Spot is a separate market domain that reuses AITOS market intelligence and the
shared capital-protection boundary. It does not reuse the Futures order adapter,
leverage, funding, liquidation or reduce-only assumptions. Options remain a
market-identity extension point only; no Options strategy or execution exists.

Capital authorization uses the USD numeraire. The first Spot rollout accepts
USDT-quoted instruments only and converts allocation budgets with the explicit
`AITOS_SPOT_QUOTE_TO_USD_RATE` (USD per USDT, default 1.0 peg assumption).
For live risk, update this rate from a point-in-time conversion source rather
than assuming a permanent peg. USDC, BTC-quoted and other quote assets remain
disabled until equivalent FX conversion is included in portfolio equity,
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
`MarketDataBus.subscribe(..., market_type="spot")`. The wire contract carries
an explicit `event_revision`; Spot trade/ticker/kline/book envelopes can be
persisted in ClickHouse `market_event_versions` using
`ReplacingMergeTree(event_revision)`, keyed by isolated `instrument_id` and
stable `event_id`. This preserves market identity, exchange/source timestamps,
source type, sequence, schema version and payload provenance without writing
Spot rows into legacy symbol-only Futures projection tables.

Trade, ticker, kline and depth symbols are multiplexed over four bounded
combined-stream connections. The order-book path opens the depth WebSocket and
buffers its first frame before fetching `/api/v3/depth`, bridges buffered diffs
to `lastUpdateId`, checks subsequent `pu`/update continuity, and raises
`SpotOrderBookGap` on a gap. The runtime supervisor restarts that stream and
rebootstraps snapshots; a consumer never continues with a silently stale book.

The runtime exposes explicit publishers into the existing `MarketDataBus`,
including isolated `market.spot.*` channels. The runtime health snapshot also
exposes weighted REST rate-limit state and server-time synchronization offset.
It does not start
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
asset and exchange fill timestamp. Base-asset and quote-asset commissions are
reflected in quantity/cost basis. Third-asset fees are resolved by
`SpotCommissionConversionService` from the latest historical aggregate trade
at or before the fill timestamp, within a strict five-second age bound, using
a direct or inverse Spot quote pair. Missing history/pairs remain pending; the
service never substitutes a current ticker price. Pending conversions survive
process restart. Reconciliation uses exchange trade IDs as idempotency keys.

`SpotUserDataStreamSupervisor` renews listen keys every 25 minutes, rotates a
key after keepalive failure, reconnects with bounded backoff and closes old
keys best-effort. `SpotRuntime.start_user_data_stream()` first reconciles
durable unresolved client order IDs through REST, then consumes private
execution reports and reconciles fills again. Private stream events are
notifications; REST order/trade state remains authoritative. Client order IDs and order intents are journaled before exchange
submission. Ambiguous submissions remain unresolved in that journal across
process restarts. The default Spot runtime also uses a durable capital
reservation database with a stable owner identity so pinned exposure can be
reconciled after restart. REST calls share a weighted token-bucket limiter and
signed requests support explicit server-time offset synchronization.

### Shared strategy, portfolio and execution replay

`SpotRuntime.submit_ranked_spot_candidate()` connects the Spot-isolated
pre-filter candidate to the shared AI Kernel. It passes normalized Spot
momentum, relative volume, spread quality, volatility quality and book
imbalance, plus Futures↔Spot lead-lag as explicitly non-decision shadow
metadata. A required strategy-owned Opportunity builder supplies the actual
thesis, stop and targets; no generic stop/target is invented by the Spot
adapter. The result is checked as LONG-only and sent through the shared
CapitalGateway. `submit_strategy_candidate()` also supports already-built
shared scanner candidates. Exits require the shared ExitDecision contract and
AITOS-managed inventory; the Futures TradeLifecycle is not repurposed to
pretend Spot sells support reduce-only.

`SpotPortfolioValuator` combines the separate Spot account's quote cash,
mark-to-market value of all Spot wallet assets using explicit asset/quote marks,
and the separately supplied Futures account equity. AITOS-managed Spot
inventory is reported as a subset attribution view and is never added a second
time on top of the wallet value. External wallet holdings contribute to account
equity but are never adopted as strategy-owned inventory. Missing marks or
unresolved third-asset fees make the snapshot explicitly incomplete. The
Futures equity input must not already include Spot account cash or assets.

`aitos.spot.replay` provides depth-walking taker fills, queue-ahead passive
maker fills, partial-fill accounting, maker/taker fees, adverse market impact
and latency-aware selection of the first historical book snapshot available
after modeled order latency. Missing depth remains unfilled; the replay never
fabricates liquidity or silently assumes a full fill.

`validate_futures_spot_lead_lag_oos()` selects leader and lag on a chronological
training segment, then evaluates the frozen hypothesis out of sample. It
reports Newey-West/HAC confidence bounds, synchronous benchmark lift and
regime-specific test correlations. This validation harness is research
evidence only; it cannot modify scanner weights or promote a strategy.

## Enabling read-only Spot market data

Set:

```dotenv
AITOS_SPOT_ENABLED=true
AITOS_SPOT_TESTNET=true
AITOS_SPOT_EXECUTION_ENABLED=false
AITOS_SPOT_ALLOW_MAINNET=false
AITOS_SPOT_QUOTE_ASSET=USDT
AITOS_SPOT_QUOTE_TO_USD_RATE=1.0
AITOS_SPOT_MAX_SYMBOLS=20
```

Construct the runtime explicitly with `build_spot_runtime()`, passing the
already-initialized shared `MarketDataRepository` when durable market history
is required, then call `await runtime.start_market_data(market_bus)`. When a
repository is supplied, the runtime starts a Spot-only persistence consumer
for `market.spot.*` channels. It writes versioned canonical events only; it
does not contaminate legacy Futures `trade_ticks`, `depth_events` or
`order_book_snapshots` tables whose primary identity is symbol-only. The
consumer uses a bounded queue and flushes on shutdown with a finite timeout;
any shutdown drops are exposed in its snapshot. The runtime discovers the
highest-turnover eligible USDT Spot symbols and starts supervised trade/ticker
subscriptions for the bounded universe plus book/kline subscriptions for the
top five.
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

- Run the complete Python 3.10-3.12 CI matrix and full Market Data Contract
  Audit on the final PR head; formatting, unit tests and static checks must be
  green before this integration is treated as verified.
- Run the opt-in Binance Spot Testnet lifecycle test with dedicated testnet
  credentials, including cancel/reconcile and process-restart recovery.
- Validate the portfolio valuator against independently reconciled Spot and
  Futures account statements, including open-order locked balances and
  third-asset fee completeness.
- Replay representative historical Spot books and maker queue observations
  across symbols, volatility/liquidity regimes and walk-forward windows.
- Validate Futures↔Spot lead-lag using source-provenance historical data,
  serial-dependence-aware confidence and regime-wise OOS results before any
  signal weighting is proposed.
- Keep mainnet execution disabled until operator review accepts the above
  evidence and dedicated API permissions.

Options are intentionally not implemented. `MarketType.OPTIONS` remains
available for a future dedicated options design and strategy package.
