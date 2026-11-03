# AITOS Multi-Market V1 — Universal Trading Architecture

## Objective

AITOS is **not** a crypto bot with optional non-crypto support. Crypto is the first production asset class; the core platform is asset-class neutral.

## Supported asset-class contract

The universal `aitos.markets` package defines `AssetClass`, `Instrument`, `MarketEvent`, `ExecutionIntent`, `MarketDataAdapter` and `ExecutionAdapter`. Vendor APIs are isolated behind adapters. Strategies, intelligence, portfolio and risk code never call an exchange/broker API directly.

Initial asset classes:

- crypto
- equity
- forex
- futures
- commodity
- rates
- bond
- option
- index

## Architecture

```text
Venue / Broker / Data Provider
              |
              v
       Market Data Adapter
              |
              v
      Canonical Market Event
              |
              +------------------+
              |                  |
              v                  v
       Hot State / Bus       ClickHouse
              |              historical truth
              v                  |
      Feature / Lead-Lag <------+ 
              |
              v
       Global Market State
              |
       +------+-------+
       |              |
       v              v
   Strategy         Risk Engine
       |              |
       +------+-------+
              v
       Execution Intent
              |
              v
       Execution Adapter
              |
              v
       Venue / Broker
```

## Cross-market intelligence

The intelligence layer can compare instruments across venues and asset classes. It stores compact feature samples and computes correlation and dynamic lead/lag without making assumptions such as `DXY up => BTC short`.

Examples of future research inputs include USD indices, equity indices, volatility indices, rates/yields, gold/oil, global equity sessions, macro calendar events, crypto derivatives and on-chain flows. Provider-specific ingestion belongs outside the core contracts.

## Regime and volatility

`GlobalMarketState` is the common decision context. It exposes risk, volatility, liquidity, USD, rates, equity and crypto scores plus confidence and feature values. A deterministic baseline classifier is included; ML/RL models can replace or augment it without changing downstream strategy contracts.

## Portfolio and risk

Portfolio accounting is asset-class neutral. The risk engine enforces gross leverage and concentration limits before an execution intent can reach a venue. High-volatility regimes automatically reduce the allowed notional. Asset-class-specific margin/contract behavior is represented on `Instrument` and remains an adapter concern.

## Session and macro boundaries

`TradingCalendar` separates session logic from strategies. Crypto can use the default 24/7 behavior; equities, futures and FX can provide explicit sessions. `MacroEvent` provides a common event schedule so strategies can avoid blind execution around high-impact releases.

## Rollout

1. Keep the existing canonical crypto market-data plane as the first production adapter.
2. Route crypto strategy decisions through `ExecutionIntent` and the universal risk gate.
3. Add read-only global market adapters (DXY/equity/rates/commodities) for research and regime detection.
4. Add macro calendar and on-chain adapters.
5. Add broker/venue execution adapters one asset class at a time, starting with paper trading.
6. Enable live multi-asset execution only after per-venue contract, reconciliation, session, margin and kill-switch tests pass.

## Production invariants

- No strategy imports a vendor SDK/API client.
- No intelligence module depends on a crypto ticker format.
- Historical data is durable in ClickHouse; Redis remains bounded hot state/event transport.
- Every external adapter reports source timestamps and health/freshness telemetry.
- Every execution path passes through risk and produces an auditable intent/order lifecycle.
- Unsupported market capabilities fail explicitly; they are never silently approximated.
