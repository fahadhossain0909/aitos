# AITOS Prop-Firm & Multi-Capital Integration

## Status

The capital layer now includes:

- **Alfax** — Binance Futures-compatible REST/HMAC API adapter.
- **TradeLocker** — JWT-authenticated REST API adapter.
- **cTrader** — official OpenApiPy/Twisted adapter plus OAuth 2.0 helper.
- **MT5** — isolated Windows gateway plus Linux `OrderExecutor` client.
- ClickHouse persistence for prop accounts, rules, equity and compliance audit data.
- A routed `OrderExecutor` facade that keeps `TradeLifecycle` venue-agnostic.

## Architecture

```text
Strategy / AI / Statistical Models
                |
                v
        Risk & Governance
                |
                v
       PropComplianceEngine
                |
                v
          CapitalRouter
                |
   +------------+-------------+----------------+
   |            |             |                |
 Binance      Alfax      TradeLocker       cTrader
   |            |             |                |
   +------------+-------------+----------------+
                |
          OrderExecutor
                |
           MT5 Gateway
                |
          Windows MT5
```

The strategy layer does not know which capital venue is used. A venue must implement the existing `OrderExecutor` contract.

## Account state

`AccountSnapshot` tracks balance, equity, start-of-day equity, high-water mark, daily loss, drawdown, and realized/unrealized PnL.

`PropRuleSet` holds account-specific constraints. The compliance engine fails closed before execution when API/automation is disallowed, daily loss would be breached, maximum drawdown would be breached, or an order exceeds a configured notional cap.

## Adapters

### Alfax

`aitos.execution.prop_adapters.AlfaxOrderExecutor` reuses the existing Binance Futures executor because Alfax documents byte-compatible Binance Futures REST/HMAC paths.

Default endpoint: `https://api.alfax.trade`.

Source: https://alfax.trade/docs

### TradeLocker

`TradeLockerOrderExecutor` implements JWT authentication and order placement. `accountId`, `accNum`, `routeId`, and `tradableInstrumentId` are explicit configuration values.

Default live endpoint: `https://live.tradelocker.com/backend-api`.

Source: https://public-api.tradelocker.com/docs/getting-started

### cTrader

`aitos.execution.ctrader_auth.CTraderOAuthClient` handles authorization-code exchange and refresh. `aitos.execution.ctrader_executor.CTraderOrderExecutor` uses the official `ctrader-open-api` Python SDK and its Protobuf transport.

Official documentation: https://help.ctrader.com/open-api/

### MT5

`aitos.execution.mt5_gateway.MT5GatewayOrderExecutor` talks to `services/mt5_gateway/server.py`. The gateway owns the MetaTrader terminal on Windows so the Linux AITOS core stays platform-neutral. The first gateway implementation intentionally allows MARKET orders only.

## Bootstrap

Existing application wiring can opt into routing without changing strategies:

```python
from aitos.capital.bootstrap import wrap_executor

executor = wrap_executor(existing_executor, capital_router)
components = await build_system(..., order_executor=executor)
```

The `RoutedOrderExecutor` remains behind the existing `OrderExecutor` contract.

## Persistence

`PropCapitalRepository` creates append-oriented ClickHouse tables for:

- `prop_accounts`
- `prop_rule_snapshots`
- `prop_equity_snapshots`
- `prop_orders`
- `prop_compliance_events`
- `prop_payouts`

This makes funded/evaluation lifecycle, drawdown state and compliance decisions auditable without mixing them into market-data tables.

## Safety rules

1. Never store provider secrets in source control.
2. Keep `SIMULATED_FUNDED` distinct from `LIVE_FUNDED`.
3. Configure rules per exact account/product/stage.
4. Paper/demo verification precedes funded execution.
5. UI/browser automation is not an execution adapter.
6. MT5 gateway must be private-network-only and bearer-authenticated.
7. Reconcile account state before enabling unattended funded trading.

## Remaining production gates

1. Wire venue/account discovery and equity polling into the runtime scheduler.
2. Add order/fill reconciliation workers for each venue.
3. Add venue health and rate-limit telemetry.
4. Verify provider-specific rules against each exact funded contract.
5. Run demo/evaluation soak tests before `LIVE_FUNDED` is permitted.
