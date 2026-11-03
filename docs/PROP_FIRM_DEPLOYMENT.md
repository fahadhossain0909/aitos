# AITOS Prop-Firm Deployment Runbook

## 1. Crypto API-first

### Alfax

Alfax exposes a Binance Futures-compatible API. AITOS can reuse its Binance execution semantics by changing the API base URL. See the official documentation at https://alfax.trade/docs.

Required secret material:

```text
ALFAX_API_KEY
ALFAX_API_SECRET
```

Do not commit either value. Register the account in `CapitalRouter` with an `AccountSnapshot` and a `PropFirmProfile` whose rules match the specific challenge/funded contract.

## 2. cTrader prop accounts

cTrader Open API supports real-time market data, trading operations, and current/pending order and position retrieval. See https://help.ctrader.com/open-api/.

Register an application in the cTrader Open API portal, configure its redirect URI, and use OAuth 2.0 to obtain/refresh the account token.

AITOS now provides:

- `aitos.execution.ctrader_auth.CTraderOAuthClient`
- `aitos.execution.ctrader_executor.CTraderOrderExecutor`

Install the official SDK from PyPI with `pip install ctrader-open-api`.

The executor uses numeric broker-specific `symbolId` values and converts AITOS quantities to cTrader protocol volume units (0.01 of a unit). The official protocol defines `ProtoOANewOrderReq` as the trading request and requires the account ID, symbol ID, order type, trade side and volume.

## 3. TradeLocker

TradeLocker exposes REST endpoints for orders, positions and account information. Trading requests require `accNum` and an instrument-specific `TRADE` route ID; market orders use IOC validity and price 0. See https://public-api.tradelocker.com/docs/getting-started.

AITOS provides `TradeLockerOrderExecutor`. Before production use, fetch the account's current instrument/route metadata from TradeLocker's `/trade/accounts/{accountId}/instruments` endpoint instead of hard-coding it from a symbol name. Rate limits should be read from `/trade/config` at startup.

## 4. MT5

MT5 is intentionally isolated behind a Windows gateway. The Linux AITOS core talks to `MT5GatewayOrderExecutor` over authenticated HTTP. The Windows gateway owns the MetaTrader 5 terminal and the `MetaTrader5` Python package.

```text
Linux AITOS
    |
    | Bearer token / private network
    v
Windows MT5 Gateway
    |
    v
MetaTrader 5 Terminal
    |
    v
Prop Broker / Prop Firm
```

Gateway environment:

```text
AITOS_MT5_GATEWAY_TOKEN=<long random secret>
AITOS_MT5_GATEWAY_HOST=0.0.0.0
AITOS_MT5_GATEWAY_PORT=8787
```

Do not expose port 8787 to the public internet. Prefer a private VPN/VPC path and firewall the gateway to the AITOS host only.

The first gateway implementation intentionally permits **MARKET orders only**. Limit/stop routing, SL/TP amendment and reconciliation must be added only after the market-order path is proven on a demo/evaluation account.

## 5. Capital and compliance lifecycle

```text
Signal
  -> Risk Engine
  -> PropComplianceEngine
  -> CapitalRouter
  -> Venue Adapter
  -> Order
  -> Reconciliation
  -> ClickHouse audit
```

The compliance layer must be configured per account. Do not assume that a prop firm's rules are universal across products, platforms, evaluation stages or funded stages.

## 6. Go-live gates

1. Demo/paper execution succeeds.
2. Account equity and positions reconcile continuously.
3. Daily-loss and max-drawdown calculations match the provider's own dashboard.
4. Every rejected order is persisted with a reason.
5. Every accepted order receives a durable client/order/fill record.
6. Gateway/API health and rate-limit headroom are monitored.
7. Only then may `LIVE_FUNDED` be enabled.
