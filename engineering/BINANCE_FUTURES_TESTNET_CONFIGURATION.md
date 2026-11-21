# Binance USDⓈ-M Futures Testnet Configuration

**Status:** endpoint and credential selection is implemented; this document does not certify an exchange order lifecycle or authorize live trading.

## Environment selection

The standalone market-data adapter defaults to **mainnet** when neither mode variable is set. Settings-driven paper/live entrypoints default to testnet through `BinanceSettings.testnet=True`. The explicit `AITOS_BINANCE_FUTURES_ENVIRONMENT` variable takes precedence for all settings-driven and adapter-created Futures components. For backward compatibility, if it is unset, the existing `BINANCE_TESTNET=true/false` flag selects `testnet`/`mainnet`. The repository's `.env.example` retains `BINANCE_TESTNET=true`, so a normal local paper configuration resolves to demo/testnet endpoints. Select testnet explicitly:

```dotenv
AITOS_BINANCE_FUTURES_ENVIRONMENT=testnet
BINANCE_FUTURES_TESTNET_API_KEY=<testnet-api-key>
BINANCE_FUTURES_TESTNET_API_SECRET=<testnet-api-secret>
```

Both the market-data adapter and live-order executor now use the documented USDⓈ-M Futures demo REST host when testnet is selected. The market-data adapter uses the following testnet endpoints unless explicitly overridden:

| Purpose | Default |
|---|---|
| REST (market data and order executor) | `https://demo-fapi.binance.com` |
| Market WebSocket combined stream | `wss://demo-fstream.binance.com/market/stream` |
| Market WebSocket raw stream | `wss://demo-fstream.binance.com/market/ws` |

These are the USDⓈ-M Futures demo/testnet endpoints documented by Binance. The market stream path follows Binance's current public/market/private WebSocket routing layout.

## Credential isolation

- Testnet mode reads only `BINANCE_FUTURES_TESTNET_API_KEY` and `BINANCE_FUTURES_TESTNET_API_SECRET`.
- It never falls back to `BINANCE_API_KEY` or `BINANCE_API_SECRET`.
- Mainnet mode retains the existing `BINANCE_API_KEY` and `BINANCE_API_SECRET` variables.
- `AITOS_BINANCE_FUTURES_ENVIRONMENT=mainnet` explicitly overrides `BINANCE_TESTNET=true`.
- The live-order preparation path selects credentials through `BinanceSettings.execution_credentials`, so it cannot silently use mainnet credentials for a testnet session.
- Constructor-injected credentials remain supported for controlled tests and dependency injection.
- Do not reuse mainnet credentials in a testnet environment. Keep credentials out of source control, logs, issue reports and CI artifacts.

## Endpoint overrides

For controlled regional or future endpoint changes, testnet-only overrides are available:

- `AITOS_BINANCE_FUTURES_TESTNET_REST_BASE_URL`
- `AITOS_BINANCE_FUTURES_TESTNET_WS_BASE_URL`
- `AITOS_BINANCE_FUTURES_TESTNET_WS_COMBINED_URL`
- `AITOS_BINANCE_FUTURES_TESTNET_WS_RAW_URL`

The base WebSocket override defaults to `wss://demo-fstream.binance.com`; combined and raw paths are appended as `/market/stream` and `/market/ws`. Explicit combined/raw overrides take precedence. Do not point a testnet-labelled deployment at a mainnet endpoint.

## Safety and acceptance boundary

Current automated coverage verifies:
- default testnet endpoint selection;
- separate testnet credential lookup and no fallback to mainnet environment credentials;
- rejection of unknown environment names;
- settings-level selection of the correct mainnet/testnet credential pair;
- live-order preparation uses the same environment for signed order execution and exchange metadata lookup;
- REST market-data routing to the demo endpoint;
- market WebSocket routing to the demo market stream.

This is **not** proof of:
- authenticated account snapshot correctness;
- order placement/cancel/replace behavior;
- partial-fill and protective-order recovery;
- process-restart reconciliation;
- fees/funding settlement;
- testnet account permissions or regional availability.

Those require a separate isolated testnet lifecycle acceptance run with dedicated testnet credentials, durable intent journaling, fault injection and reconciliation evidence. The paper runner continues to use its simulated execution adapter; selecting a market-data environment does not switch paper execution to venue orders. No production deployment, VPS activation or live order was performed by this change.

## Read-only CI preflight trigger

The Three-Store workflow is currently introduced on the audit feature branch, not
on the repository default branch. GitHub may therefore omit the **Run workflow**
control for this branch-only workflow even though its YAML declares
`workflow_dispatch`. Do not merge the Draft PR or change the default branch just
to expose that control.

The workflow also supports a deliberate push-only opt-in for its authenticated,
read-only account inventory preflight: a commit pushed to
`audit/system-audit-and-docs-2026-11` whose commit message contains
`[testnet-readonly]` runs that preflight job. Ordinary pushes run the recovery
integration job but skip the credentialed preflight. The opt-in job requires
the dedicated `BINANCE_FUTURES_TESTNET_API_KEY` and
`BINANCE_FUTURES_TESTNET_API_SECRET` GitHub Actions secrets and performs no
order placement, cancellation, amendment or other write operation.

This marker is not authorization for write-side testnet acceptance. Entry,
protection, partial-fill and exit tests remain disabled until a separate
explicit operational approval and bounded test-order budget are provided.
