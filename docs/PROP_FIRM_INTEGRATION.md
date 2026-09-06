# AITOS Prop-Firm & Multi-Capital Integration

## Status

This phase adds the provider-neutral capital layer, a fail-closed prop compliance firewall, capital routing, and API-backed adapters for the two currently verified API shapes below:

- **Alfax** — Binance Futures-compatible REST/HMAC API.
- **TradeLocker** — JWT-authenticated REST API.

cTrader is intentionally represented by the same `OrderExecutor` boundary but is not bundled as a fake implementation: its official API uses an application registration + OAuth 2.0 flow and a TCP/WebSocket/Protobuf transport. It should be added as a dedicated adapter after the `ctrader-open-api` SDK is introduced as an explicit optional dependency.

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
        +-------+---------+
        |       |         |
     Binance  Alfax   TradeLocker
        |       |         |
        +-------+---------+
                |
          OrderExecutor
```

The strategy layer does not know which capital venue is used. A venue must implement the existing `OrderExecutor` contract.

## Account state

`AccountSnapshot` tracks:

- balance and equity;
- start-of-day equity;
- high-water mark;
- daily loss;
- drawdown;
- realized/unrealized PnL.

`PropRuleSet` holds account-specific constraints. The compliance engine fails closed before execution when API/automation is disallowed, daily loss would be breached, maximum drawdown would be breached, or an order exceeds a configured notional cap.

## Adapters

### Alfax

`aitos.execution.prop_adapters.AlfaxOrderExecutor` reuses the existing Binance Futures executor because Alfax documents byte-compatible Binance Futures REST/HMAC paths. Only the base URL changes.

Default endpoint: `https://api.alfax.trade`.

Source: https://alfax.trade/docs

### TradeLocker

`TradeLockerOrderExecutor` implements the documented JWT login flow and order endpoint. Because TradeLocker requires `accountId`, `accNum`, `routeId`, and `tradableInstrumentId`, these values are explicit configuration rather than guessed from a symbol.

Default live endpoint: `https://live.tradelocker.com/backend-api`.

Source: https://public-api.tradelocker.com/docs/getting-started

### cTrader

Use the same `OrderExecutor` boundary. cTrader Open API supports trading operations and Python SDKs, but its application/account authentication and transport are materially different from a REST adapter. Do not emulate it with UI automation.

Official documentation: https://help.ctrader.com/open-api/

## Safety rules

1. Never store provider secrets in source control.
2. Keep `SIMULATED_FUNDED` distinct from `LIVE_FUNDED` in accounting and reporting.
3. Do not enable a venue merely because an API exists; the provider's automation, evaluation, drawdown, news, overnight, weekend, and rate-limit rules must be represented in `PropRuleSet`.
4. Paper/sandbox verification comes before live credentials.
5. UI/browser automation is not an execution adapter.

## Next implementation layers

1. Wire `CapitalRouter` into `TradeLifecycle` behind the existing governance gate.
2. Add durable ClickHouse tables for prop accounts, rule snapshots, compliance decisions, orders, fills, equity, drawdown and payouts.
3. Add cTrader Open API adapter with OAuth token refresh and account/instrument mapping.
4. Add MT5 gateway as a separate process rather than embedding a terminal into the Linux AITOS core.
5. Add venue health, rate-limit and reconciliation workers before enabling unattended funded trading.
