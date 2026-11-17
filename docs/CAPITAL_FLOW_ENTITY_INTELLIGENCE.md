# AITOS Capital Flow & Entity Intelligence (CFEI)

## Mission and non-goals

CFEI is a read-only research subsystem for observing large capital movement, public market positioning and disclosed institutional ownership. It normalizes evidence into a provider-neutral event model so that crypto, equities, FX and future markets can share an intelligence interface.

It is not an identity-discovery service. It does not deanonymize wallet owners, access private exchange accounts, scrape private data, place orders, or modify AITOS strategy, risk, sizing, stops or execution. No CFEI score is a calibrated probability until a separate out-of-sample calibration study demonstrates that property.

## Implemented code in this branch

### 1. Provider-neutral contracts
- Versioned `CapitalEvent`, `EntityEvidence` and `EntityLabel` contracts.
- UTC-aware event and evidence timestamps, finite numeric validation, provenance and deterministic event deduplication key.
- Relationship confidence and operator labels are separate concepts. A high-confidence heuristic is not proof of common ownership.

### 2. Binance USD-M public positioning
- Public top-trader position ratio and account ratio endpoints.
- Public taker buy/sell volume ratio endpoint.
- Bounded symbol set and five-minute overlap cursor to tolerate delayed updates.
- Values explicitly labeled as aggregate cohorts. Binance public endpoints do not expose a list of individual top trader account IDs or their private positions. Therefore CFEI must not pretend that an aggregate "top trader long/short ratio" identifies a specific whale.

### 3. EVM token transfers and DEX pool swaps
- HTTPS JSON-RPC only, bounded block windows, configured contract/pool allowlists and confirmation-depth delay.
- ERC-20 Transfer logs preserve raw base-unit amounts in metadata; token decimals and USD value are not guessed.
- Uniswap V2/V3-compatible Swap log decoding is opt-in per configured pool. The event preserves signed token0/token1 deltas and pool identity.
- EVM event time is taken from the canonical block timestamp, not the collector's wall clock.
- Pool caller/sender can be a router or aggregator; it is not necessarily the beneficial trader.

### 4. Solana address activity
- Standard finalized JSON-RPC, bounded pagination and a separate signature cursor per configured address.
- SPL token and native SOL balance deltas are derived from pre/post transaction balances for the configured owner address.
- Failed transactions do not generate capital-flow events.
- If a stored signature falls outside the bounded scan window, the adapter fails closed and requires a controlled backfill instead of silently skipping history.

### 5. Durable ingestion and recovery
- Redis per-source lease with random fencing token and Lua compare-and-set renewal/release/checkpoint.
- Heartbeat renewal during slow polling. Stale workers cannot advance a cursor after lease loss.
- Event append happens before cursor commit. A crash between those operations causes replay, not data loss.
- Delivery is at-least-once. Consumers and analytics must deduplicate using `deduplication_key`; this is not exactly-once delivery.
- ClickHouse append-only event store uses a stable replacing key. Queries requiring deduplicated rows should use `FINAL` or an explicit latest-row aggregation.

### 6. Entity evidence and point-in-time valuation
- Evidence graph retains candidate and verified relations separately.
- Only direct operator-verified relationships are returned as cluster links; no transitive automatic wallet merging.
- Price valuation is injected through a point-in-time quote interface. Stale/future quotes are rejected.
- Flow direction must be explicit in source metadata. An exchange deposit, withdrawal, bridge transfer or LP movement is not inherently a buy or sell.

### 7. Opt-in isolated runtime
The standalone runner is `scripts/run_capital_intelligence.py`. Docker Compose exposes it only under the `capital-intelligence` profile. It is disabled by default and does not import trading execution modules.

Enable only after configuring the public endpoints and allowlists in a private `.env` file:

```dotenv
AITOS_CFEI_ENABLED=true
AITOS_CFEI_POLL_SECONDS=15
AITOS_CFEI_BINANCE_SYMBOLS=["BTCUSDT","ETHUSDT"]
AITOS_CFEI_EVM=[{"rpc_url":"https://YOUR_PUBLIC_RPC","chain_id":"1","contracts":["0xYOUR_TOKEN_CONTRACT"],"confirmations":20}]
AITOS_CFEI_DEX_POOLS=[{"rpc_url":"https://YOUR_PUBLIC_RPC","chain_id":"1","pools":{"0xYOUR_POOL":"uniswap_v3"},"confirmations":20}]
AITOS_CFEI_SOLANA=[{"rpc_url":"https://YOUR_SOLANA_RPC","addresses":["YOUR_PUBLIC_WALLET_ADDRESS"]}]
```

Start the isolated observer:

```bash
docker compose --profile capital-intelligence up -d aitos-capital-intelligence
docker compose logs -f aitos-capital-intelligence
```

Metrics are exposed on the internal container port 8121 at `/metrics`. Do not expose RPC URLs containing secrets or provider keys in public dashboards/logs. Use an RPC URL secret manager in production rather than committing credentials.

### 8. Resource policy
The optional observer has a conservative default CPU limit of 0.10 cores and memory limit of 384 MiB. Keep symbol, pool and address allowlists small; batch and poll budgets should be increased only after measuring RPC latency, rate limits and ClickHouse write load. The observer profile is separate from paper/live services and is not automatically started by the default Compose profile.

## Explicitly not complete / production gates

These are not claimed as implemented or validated by this PR:
- Reorg rollback and canonical-chain reconciliation beyond configured confirmation depth. Operators must run conservative confirmation depths; a deep reorg requires a backfill/rebuild procedure.
- Full ERC-20 decimals/token registry, point-in-time FX/USD pricing adapters, bridge and wrapped-asset normalization.
- Complete Solana address attribution across token accounts, CPI, inner instructions, compressed assets, stake accounts and program-specific transfers.
- Automatic wallet clustering, exchange/custody attribution and beneficial-owner identification. Those require multiple independent evidence sources and human review.
- Full DEX coverage across every protocol, pool factory and chain. Only explicitly configured Uniswap V2/V3-compatible pools are decoded.
- SEC 13F/Form 4, CFTC COT, forex dealer positioning and global institutional holdings ingestion. The generic contract is market-neutral, but those source adapters and their legal/temporal semantics require a separate implementation.
- Dashboard/API integration, alert routing and integration into World Intelligence context.
- Historical data backfill, replay, walk-forward evaluation, false-positive analysis, model calibration and measured predictive lift.
- Production runtime deployment, provider-specific rate-budget tests, ClickHouse/Redis integration tests and operator runbooks for disaster recovery.

## Research and model governance

CFEI outputs should first be stored as observations, not strategy signals. Before any strategy consumer is approved:
1. Build point-in-time datasets with publication/observation timestamps and survivorship controls.
2. Label subsequent market outcomes at predeclared horizons.
3. Split by time and market regime; prohibit random row splits and look-ahead.
4. Compare a baseline without CFEI against baseline + each feature family.
5. Report precision/recall for alerts, calibration error, information coefficient, net-of-fee return, drawdown, turnover and coverage.
6. Run ablations by source, entity type, chain, liquidity tier and event type.
7. Require human approval, shadow/paper canary, rollback and model-version lineage.
8. Only then consider CFEI as a bounded advisory feature. It must never override risk controls.

## Cross-market architecture

The event schema carries market, venue, instrument, source and entity evidence independently. Future adapters can normalize SEC EDGAR filings (13F and Forms 3/4/5), CFTC reports and official exchange disclosures. Filing publication time, reporting period, amendment status, reporting lag and beneficial-ownership scope must remain explicit. A 13F holding is not a real-time position, and a reported insider transaction is not an automatic directional signal.
