# AITOS Binance Spot Integration

## Status and safety

This package is an opt-in Spot domain isolated from the Futures adapter and executor.
Importing the package does not start a connection or enable order placement. The
executor defaults to disabled and the adapter defaults to Binance Spot Testnet.
Mainnet execution requires both an explicitly mainnet-configured adapter and
`allow_mainnet=True`. Production activation remains an operator decision.

## Components

- `aitos.spot.models`: Decimal-safe instrument, balance, order and fill contracts.
- `aitos.spot.filters`: Spot `exchangeInfo` parsing and PRICE_FILTER, LOT_SIZE,
  MARKET_LOT_SIZE and NOTIONAL validation.
- `aitos.spot.binance`: public market data, signed account, order query/place/cancel.
- `aitos.spot.execution`: explicit execution guard, symbol capability and filter checks.
- `aitos.spot.inventory`: SQLite fill ledger, idempotent fill keys, base/quote fee handling.
- `aitos.spot.scanner`: deterministic explainable liquidity/momentum pre-ranking only.

## Important invariants

1. Spot inventory is AITOS-managed inventory, not the entire exchange wallet balance.
   External balances are never automatically adopted for selling.
2. Reconcile order status and individual trade fills after uncertain network outcomes.
   A client order ID is the idempotency key; never blindly retry an ambiguous POST.
3. The inventory ledger accepts a fill once, rejects selling more than managed inventory,
   and records commission asset so base-asset and quote-asset fees are accounted for.
4. Quote-quantity market buys are only accepted when the symbol advertises support.
5. Decimal arithmetic is used for exchange steps and monetary accounting.
6. Spot and Futures instruments are distinct identities; portfolio aggregation should
   group them by underlying asset while retaining venue/market identity.
7. Futures-only signals (funding, OI, liquidation) are optional cross-market evidence,
   never fabricated as zero for Spot-only instruments.
8. The scanner output is not an entry signal. Existing Strategy/AI governance, capital
   reservation, portfolio risk, and thesis/exit approval must remain upstream.
9. Options remain a disabled extension point; no options order or strategy is implemented.

## API surface

REST endpoints used: `/api/v3/exchangeInfo`, `/api/v3/klines`,
`/api/v3/depth`, `/api/v3/ticker/24hr`, `/api/v3/account`,
`/api/v3/order`. Private calls require API credentials with least-privilege
permissions. Withdrawals should be disabled on the exchange key.

## Verification

Run `pytest tests/spot -q`. Use Spot Testnet for authenticated order lifecycle
tests. Testnet order book liquidity is synthetic and cannot validate live slippage,
profitability, or strategy edge. Do not enable mainnet until reconciliation,
restart recovery, fees, market impact and portfolio risk integration have been
verified in the deployment environment.
