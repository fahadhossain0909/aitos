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
- Uniswap V2/V3-compatible pool swaps and Uniswap V4 singleton PoolManager swaps are opt-in and decoded from their protocol-specific event layouts. V4 pool IDs are bytes32 identifiers, not pool contract addresses; operators must allowlist the PoolManager and provide a reviewed `v4_pool_tokens` map (pool ID -> sorted token0/token1 currencies). Native currency is represented by the zero address. Signed token deltas, post-swap price/tick/liquidity and fee are preserved. V4 `Initialize` and `ModifyLiquidity` events are discovered by the manager observer. Liquidity add/remove events retain signed liquidity delta, tick range, sender and salt as evidence only; they are not converted into token flow or USD notional. Discovery never auto-activates a pool.
- EVM event time is taken from the canonical block timestamp, not the collector's wall clock.
- Pool caller/sender can be a router or aggregator; it is not necessarily the beneficial trader.

### 4. Solana address activity
- Standard finalized JSON-RPC, bounded pagination and a separate signature cursor per configured address.
- SPL token and native SOL balance deltas are derived from pre/post transaction balances for the configured owner address.
- Parsed outer and inner/CPI SPL `transfer` / `transferChecked` instructions are attached as counterparty evidence when RPC token-account owner metadata is available. Parsed System Program SOL transfers and Stake Program lifecycle instructions (delegate, deactivate, withdraw, split, merge, initialize and authority/lockup changes) are also retained as evidence only. They do not create a second flow event, avoiding double-counting balance deltas.
- The authority/signer is not assumed to be the beneficial owner. Counterparty owner references are only as strong as the RPC-provided token-account ownership metadata.
- Failed transactions do not generate capital-flow events.
- If a stored signature falls outside the bounded scan window, the adapter fails closed and requires a controlled backfill instead of silently skipping history.

### 5. Durable ingestion and recovery
- Redis per-source lease with random fencing token and Lua compare-and-set renewal/release/checkpoint.
- Heartbeat renewal during slow polling. Stale workers cannot advance a cursor after lease loss.
- Event append happens before cursor commit. A crash between those operations causes replay, not data loss.
- Delivery is at-least-once. Consumers and analytics must deduplicate using `deduplication_key`; this is not exactly-once delivery.
- ClickHouse append-only event store uses a stable replacing key. Queries requiring deduplicated rows should use `FINAL` or an explicit latest-row aggregation.

### 6. Entity evidence, candidate ranking and point-in-time valuation
- Evidence and temporal labels are persisted in dedicated ClickHouse tables. Stable evidence/label identifiers make replay idempotent; the graph never promotes candidate evidence.
- Only direct operator-attested links are returned as verified cluster links; no transitive automatic wallet merging.
- Explainable address-level candidate ranking uses gross valued flow, recurrence, counterparty breadth, source diversity, explicit direction coverage, market-liquidity impact and evidence confidence. Entity IDs are chain-scoped as `chain:address` to prevent same-address collisions across networks. Explicitly directed transfers contribute an outgoing perspective for the sender and the inverse incoming perspective for the recipient, so both sides can be prioritized without duplicating persisted transfer events. Each component is exposed with caveats. Aggregate Binance cohort rows are excluded because they do not identify individual traders.
- The score is a heuristic analyst-prioritization index, not a probability, beneficial-owner attribution, investment recommendation or trading signal.
- Token decimals and asset identity come only from the configured token registry. Raw integer amounts are retained in metadata and normalized using decimal arithmetic.
- Binance spot 1-minute historical quotes use only candles whose close time is at or before the event time. Future and stale quotes are rejected. USDT is not silently treated as USD; setting `AITOS_CFEI_ALLOW_USDT_USD_PROXY=true` explicitly permits a labeled USDT proxy, otherwise `usd_value` remains unset for USDT quotes.
- Flow direction must be explicit in source metadata. An exchange deposit, withdrawal, bridge transfer or LP movement is not inherently a buy or sell.

### 7. Official CFTC aggregate positioning
- The optional adapter reads the official CFTC Public Reporting Socrata endpoint over HTTPS using a required, operator-supplied dataset ID and explicit field mapping.
- Each row becomes an aggregate market-positioning observation with open interest and long/short/net contracts by report category. These are trader-class aggregates, never individual whales or wallet identities; they are excluded from address-level ranking.
- The source's report date is retained as event time and local poll time as observation time. If the selected dataset does not expose its publication timestamp, the event is explicitly marked as such; do not treat it as point-in-time tradable information without a verified release calendar.
- Incremental ingestion uses a durable bounded offset cursor over stable report-date/market-code ordering. In addition, each poll scans a configurable number of pages from the beginning of the dataset (`revision_refresh_pages_per_poll`, default 5). This overlapping bounded refresh re-emits stable report-date/market-code event IDs so corrected historical rows can replace prior ClickHouse versions without resetting the live cursor. It is a best-effort revision detector, not a source-provided revision feed; full historical reconciliation still requires a controlled backfill.

Configure a dataset and exact column mapping, for example:

```dotenv
AITOS_CFEI_CFTC_COT=[{"dataset_id":"<official-dataset-id>","report_type":"legacy_futures_only","field_map":{"report_date":"report_date_as_yyyy_mm_dd","market_code":"cftc_contract_market_code","market_name":"market_and_exchange_names","open_interest":"open_interest_all","categories":{"noncommercial":{"long":"noncomm_positions_long_all","short":"noncomm_positions_short_all"},"commercial":{"long":"comm_positions_long_all","short":"comm_positions_short_all"},"nonreportable":{"long":"nonrept_positions_long_all","short":"nonrept_positions_short_all"}},"revision_refresh_pages_per_poll":5}]
```

The field names vary by CFTC report family; validate them against the selected official dataset before enabling ingestion. Polling is throttled to at least once per hour.

### 8. Official SEC EDGAR institutional and insider disclosures
- The optional SEC adapter reads official EDGAR submissions, 13F-HR information tables and Forms 3/4/5 ownership XML for an explicit CIK allowlist.
- 13F holdings retain both the report-period date and the later filing-publication date. The reported value is converted from the filing's USD-thousands unit. A 13F record is a delayed quarterly disclosure, not a current portfolio position.
- Form 4 records preserve transaction date, filing date, reporting-owner CIK, issuer CIK, transaction code, acquired/disposed code and reported price/shares where available. Amendments remain separately identifiable by form and accession.
- The adapter requires a contactable SEC User-Agent (including an email address), bounds the number of filings per poll, respects a conservative request interval, rejects oversized/DTD-bearing XML and fails closed if its cursor falls outside the SEC recent-submissions window. It also re-fetches a configurable small number of latest known accessions each poll (default 5) to detect corrected/revised source documents without moving the durable new-filing cursor backwards. New `/A` amendments remain distinct accessions and are ingested normally. This is not a substitute for traversing the SEC historical submissions archive.
- No owner is inferred beyond the CIK explicitly present in the filing. Insider transactions and institutional holdings are disclosures, not standalone directional signals.

Configure it with:

```dotenv
AITOS_CFEI_SEC_CIKS=["0000320193","0000789019"]
AITOS_CFEI_SEC_USER_AGENT="AITOS research team contact@your-domain.example"
AITOS_CFEI_SEC_MAX_FILINGS_PER_POLL=25
AITOS_CFEI_SEC_REVISION_RESCAN_FILINGS=5
```

The CIKs above are examples only; replace them with an intentionally selected monitoring list and a real contact address. The SEC adapter is optional and uses the same append-before-checkpoint worker.

### 9. Opt-in isolated runtime
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

### 10. Resource policy
The optional observer has a conservative default CPU limit of 0.10 cores and memory limit of 384 MiB. Keep symbol, pool and address allowlists small; batch and poll budgets should be increased only after measuring RPC latency, rate limits and ClickHouse write load. The observer profile is separate from paper/live services and is not automatically started by the default Compose profile.

## Explicitly not complete / production gates

These are not claimed as implemented or validated by this PR:
- EVM observers retain a contiguous recent block-hash window, detect head reorgs, search for a common ancestor and rescan the affected range. ClickHouse appends `finality=reverted` versions for orphaned logs before the cursor advances. Reorgs deeper than the configured window (default 64 blocks), RPC inconsistencies or incomplete canonical headers fail closed and still require controlled backfill/rebuild.
- On-chain token metadata discovery and broad token registry population. The current registry is explicit/operator-configured; unsupported tokens remain unvalued.
- Complete Solana attribution across all token-account ownership edge cases, compressed assets, all stake-account lifecycle/authority edge cases, arbitrary program-specific CPI semantics and historical transaction versions. Parsed outer/CPI SPL and System Program transfer evidence plus a bounded set of parsed Stake Program lifecycle instructions are attached as supporting evidence only; balance deltas remain separate and can include fees, rent, mint/burn, swaps or program effects.
- Automatic wallet clustering, exchange/custody attribution and beneficial-owner identification. Those require multiple independent evidence sources and human review.
- Full DEX coverage across every protocol, pool factory and chain. V2/V3-compatible factory discovery and configured-pool swaps are implemented; V4 and protocol-specific variants remain unsupported. Discovered pools are never auto-activated.
- Forex dealer positioning, exchange-specific institutional holdings and broader global disclosure ingestion. The CFTC adapter is schema-mapped and aggregate-only; dataset-specific field mapping, historical refresh and publication-calendar validation remain operator gates. SEC 13F-HR and Forms 3/4/5 are implemented, but production-scale CIK coverage and backfill are not yet validated.
- A bounded, read-only, bearer-token-protected per-entity explorer API is implemented alongside the metrics listener. A dedicated entity dashboard, watchlist mutation workflow and external alert delivery remain open.
- The CFEI-to-World-Intelligence bridge is optionally wired into a durable WI ingestion worker using the canonical AITOS Redis EventBus, ClickHouse WI store, Redis source leases and fenced cursor persistence. It remains disabled by default; forwarded events are neutral/advisory and Scanner/AI Kernel feature integration remains deliberately disabled.
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


## Additional market-structure and evidence-graph primitives

The optional `BinanceMarketStructureAdapter` adds public USD-M aggregate-trade,
open-interest, premium-index/funding and bounded depth-snapshot observations.
Configure it independently from the aggregate cohort adapter:

```dotenv
AITOS_CFEI_BINANCE_MARKET_SYMBOLS=["BTCUSDT","ETHUSDT"]
AITOS_CFEI_LARGE_TRADE_USD=100000
```

The collector uses per-symbol aggregate-trade IDs for incremental polling after
its initial bounded snapshot. The first poll is deliberately limited to the
latest 1,000 aggregate trades per symbol; operators requiring complete history
must run a separate historical backfill. Depth is an L2 snapshot (not L3 order
identity/cancel-replenishment telemetry). The USD threshold is quote-notional
approximation and must not be interpreted as an exact USD conversion for every
quote asset. API rate budgets must be measured before increasing symbols.

The `intelligence.py` module provides explainable candidate-link suggestions
and descriptive flow behavior profiles. Shared counterparties only produce
candidate evidence; they cannot establish common ownership. Accumulation and
distribution scores are deterministic heuristics, not probabilities, trained
models or execution signals. Reverted events are excluded.

The optional `neo4j_graph.py` adapter persists event observations, labels and
relationship evidence using parameterized Cypher. Event nodes are keyed by the
stable event deduplication key; actor/counterparty references are namespaced
by venue and do not merge identities across venues. The worker writes ClickHouse
first, then the optional Neo4j sink, and only then commits the Redis cursor. A
Neo4j failure therefore causes safe at-least-once replay; ClickHouse uses stable
deduplication keys and Neo4j uses MERGE constraints. The graph remains an
evidence/observation graph, not a beneficial-owner graph.


## 11. Bounded EVM token metadata discovery

The optional `EvmTokenMetadataResolver` reads ERC-20 `decimals()` and
`symbol()` from configured contract addresses over HTTPS JSON-RPC. Discovery
is bounded to 500 addresses per configured chain, uses a bounded concurrency
limit, validates ABI responses, and refuses unsupported decimal values. It
does not scan arbitrary contracts or infer token legitimacy. A returned
symbol is descriptive only and is never promoted to a Binance price-pair
symbol; price mapping remains an explicit operator decision.

Example:

```dotenv
AITOS_CFEI_EVM_TOKEN_DISCOVERY=[{"rpc_url":"https://YOUR_PUBLIC_RPC","chain_id":"1","addresses":["0xTOKEN_ADDRESS"],"max_concurrency":4}]
```

Explicit `AITOS_CFEI_TOKENS` entries override discovered metadata for the same
chain/address. RPC credentials must be supplied through deployment secrets,
not committed environment files.

## 12. Point-in-time event replay

The `replay.py` module provides a deterministic research harness:
- excludes reverted events, unknown flow direction and events observed before
  their event timestamp;
- requires a price observation at the event time and at each predeclared
  forward horizon, with a strict maximum quote-age bound;
- applies the event's explicit flow sign to forward returns; it does not infer
  direction from transfers or pool callers;
- reports sample count, directional hit rate, mean return and median return;
- offers chronological train/test splitting only; random row splitting is
  intentionally not provided. The splitter purges training rows whose forward
  label horizon plus quote-age allowance and optional embargo could overlap the
  test period.
- reports gross and cost-adjusted returns when per-side fees, round-trip spread,
  per-side slippage and pro-rata funding assumptions are supplied. Cost inputs
  are explicit and bounded; they are not inferred from the venue.

This is a label-generation and descriptive evaluation primitive, not a
strategy, probability calibration or proof of predictive lift. The cost model
is a conservative research approximation: it does not model signed realized
funding, market impact curves, queue position, partial fills or latency. Those
require venue-specific replay and execution telemetry before model governance.


## 13. DEX factory discovery and reorg recovery

The optional `EvmDexFactoryDiscoveryAdapter` scans explicitly configured V2/V3
factory contracts for canonical `PairCreated` and `PoolCreated` events. It
records the factory, pool address, token0/token1, protocol, block and log
provenance as a `dex_pool_created` observation. Factory addresses are operator
allowlisted; event topics alone do not authenticate a protocol because forks
reuse the same event ABI.

Example:

```dotenv
AITOS_CFEI_DEX_FACTORIES=[{"rpc_url":"https://YOUR_PUBLIC_RPC","chain_id":"1","factories":{"0xYOUR_V2_FACTORY":"uniswap_v2","0xYOUR_V3_FACTORY":"uniswap_v3"},"confirmations":20,"max_block_range":500,"max_reorg_depth":64,"start_block":18000000}]
```

Discovered pools are not automatically activated for swap ingestion. Review
factory provenance, token contracts, liquidity, honeypot/transfer restrictions,
and protocol deployment before explicitly adding a pool to
`AITOS_CFEI_DEX_POOLS`. This avoids turning arbitrary contracts with matching
event signatures into trusted data sources.

Both the swap adapter and factory-discovery adapter store a bounded canonical
block-hash cursor, search for a common ancestor when the prior head changes,
rescan the affected range, and publish reorg instructions. ClickHouse and
optional Neo4j event nodes are both marked `finality=reverted` before the Redis
cursor can advance. Factory discovery now has the same bounded reorg protection
as swap ingestion. Set an optional per-factory `start_block` to begin a bounded,
resumable historical discovery scan from the factory deployment block; the
Redis cursor resumes subsequent pages. Reorganizations deeper than the retained window, incomplete
RPC headers, or a recovery range larger than one configured scan fail closed
and require controlled backfill; the adapter never advances its cursor over an
unreconciled fork. Configure `max_reorg_depth` (1-256, default 64) and ensure
`max_block_range` can cover the maximum expected recovery range.


## 14. World Intelligence context bridge

`CapitalFlowIntelligenceAdapter` exposes persisted CFEI observations as
normalized World Intelligence events. It reads the ClickHouse CFEI event table
using a bounded observed-time/event-id cursor and `FINAL`, so replacing versions
and EVM reorg tombstones supersede older confirmed observations. Reverted rows
advance the cursor but are not republished as positive context.

The bridge deliberately sets sentiment to neutral and preserves source
provenance, event/observation times, venue, instrument, value, flow direction,
finality and identity caveats in metadata. It does not convert transfers into
buy/sell intent, does not claim wallet ownership, and marks
`strategy_use_approved=false`. It can be supplied as an adapter to the existing
`IntelligenceIngestionWorker`; no execution, scanner or risk integration is
enabled by this PR.

Example construction in a World Intelligence runtime:

```python
adapter = CapitalFlowIntelligenceAdapter(
    clickhouse=clickhouse_client,
    database="aitos",
    initial_lookback_hours=24,
    max_events=500,
)
worker = IntelligenceIngestionWorker(
    adapters=[adapter, *other_adapters],
    store=world_intelligence_store,
    sink=event_bus_sink,
)
```

The first run uses a bounded lookback. Operators requiring older CFEI history
must perform an explicit backfill and start the bridge with a reviewed cursor;
do not silently increase the initial lookback on a production worker.


## 15. Replay validation controls

`chronological_split` purges training rows whose longest forward label plus
the configured maximum quote-age allowance and optional embargo reaches the
test window. The default quote-age allowance is 30 seconds and must be aligned
with the replay builder's `max_price_age_seconds` when a different value is
used. The `summarize_replay` helper retains gross metrics and separately
reports net-of-cost mean return and hit rate using explicit per-side fees,
round-trip spread, per-side slippage and pro-rata funding cost assumptions.
These are transparent assumptions, not a substitute for venue-level fill replay.


## 16. Source-level operations dashboard and alerts

The observer exposes aggregate and per-source Prometheus counters/gauges:
`aitos_cfei_source_events_persisted_total`,
`aitos_cfei_source_poll_errors_total`,
`aitos_cfei_source_lease_contention_total` and
`aitos_cfei_source_last_success_unix_seconds`. A lease-contention no-op is not
counted as a successful poll and does not refresh source freshness. The
`AITOSCFEISourcePollErrors` and `AITOSCFEISourceStale` alert rules identify
the specific source ID, instead of only showing a global observer failure.
The Grafana dashboard is
`monitoring/grafana/dashboards/aitos-capital-intelligence.json`.

Source IDs are operator configuration values and appear as Prometheus labels.
Keep their cardinality bounded; do not include transaction IDs, wallet IDs or
other per-event identifiers in metric labels. Detailed event/entity analysis
belongs in ClickHouse/Neo4j, not Prometheus.


## 17. Read-only entity explorer API

The optional observer exposes a bounded entity read model and a dependency-free
analyst dashboard on the same internal HTTP listener as `/metrics`. Open
`/entities` from the trusted internal network, enter the operator bearer token
in the password field, and load the top 100 observed addresses. Selecting an
address loads its bounded event/evidence/label history. The browser keeps the
token only in page memory, sends it only to same-origin API requests, and uses
text-only DOM rendering for untrusted address/label data. The dashboard does
not mutate watchlists, labels, evidence or strategy state. Entity endpoints are disabled unless
`AITOS_CFEI_API_TOKEN` is configured with at least 32 characters. Requests
must include `Authorization: Bearer <token>`; use a secret manager in
production and do not expose the observer port publicly without network
controls.

- `GET /v1/entities?limit=50&offset=0` lists observed EVM/Solana addresses,
  gross valued flow, known inflow/outflow, source and counterparty breadth, and
  last observation time. Bounds are 1-100 rows and offset 0-10,000.
- `GET /v1/entities/{chain}/{address}` returns a bounded recent event history,
  persisted relationship evidence and temporal labels for one chain-scoped
  address. History includes both actor and counterparty perspectives, with an
  entity-relative role and flow sign on each event. Event history is capped at
  500 rows; evidence and labels are capped independently.

The explorer reads ClickHouse with `FINAL`, excludes reverted events and
filters aggregate cohorts and DEX pool-caller-only observations. Entity IDs
are chain-scoped (`chain:address`) to avoid silently merging identical
address strings across networks. The API scopes evidence and labels to the same `chain:address` key to prevent
cross-chain label leakage. Operator-supplied evidence/labels intended for this
explorer must use that chain-scoped key. The API always marks identity as
`unverified_address` and `beneficial_owner_verified=false`; a verified
operator label is not proof of beneficial ownership. The API is read-only:
watchlist mutation, label editing and evidence promotion are not exposed. Operators may
supply chain-scoped, time-bounded classifications at observer startup through
AITOS_CFEI_ENTITY_LABELS; allowed categories are exchange, custodian, bridge,
protocol, market_maker, treasury, staking, burn and other. Each record requires
an explicit source, timezone-aware valid_from, optional valid_until and an
operator-controlled verified boolean. These labels classify the observed
address only; verified does not mean beneficial ownership is proven. Labels
are append-only in this runtime; expire them with valid_until and maintain
corrections through a reviewed database maintenance process.


## 18. Walk-forward, calibration and event-family ablation

The research harness now includes expanding-window `walk_forward_splits`
with label-horizon purging, maximum quote-age allowance and optional embargo.
It also exposes `summarize_replay_by_event_type` to compare event families
under the same explicit fee, spread, slippage and funding assumptions.

`evaluate_probabilistic_predictions` reports Brier score, log loss, expected
calibration error, precision, recall and F1 at a predeclared threshold. It
accepts only externally generated probabilities and binary outcomes. The
CFEI candidate ranking score is a heuristic and must not be passed as a
probability or described as calibrated. No trained model, predictive lift,
statistical significance or out-of-sample performance is claimed without
real point-in-time historical datasets and completed fold reports.


## 19. Optional World Intelligence bridge runtime

Set `AITOS_CFEI_WI_BRIDGE_ENABLED=true` to start the CFEI-to-WI worker
inside the observer process. It reads persisted CFEI rows using the adapter's
observed-time/event-id cursor, writes normalized WI events to the dedicated
World Intelligence ClickHouse tables, and publishes through the canonical
AITOS Redis EventBus topic `intelligence.events.normalized`. The worker uses
a separate Redis lease namespace and fenced cursor checkpoints. The source ID
on each normalized WI event is stable for the adapter; the original CFEI
source ID is retained in event metadata.

The bridge is opt-in and does not start Scanner/AI Kernel subscriptions or
change strategy, risk, sizing or execution. Forwarded sentiment is neutral;
transfer direction is retained as metadata and is never promoted to news
sentiment or buy/sell intent. It is safe to disable without affecting CFEI
collection.

### Uniswap V4 operator configuration

V4 is a singleton manager, not a collection of pair contracts. Configure the
PoolManager address as the uniswap_v4 protocol in both the discovery and swap
observer allowlists. The discovery worker records Initialize events as
pool-registry evidence; the swap observer only decodes pool IDs explicitly
listed in the reviewed token map.

```dotenv
AITOS_CFEI_DEX_FACTORIES=[{"rpc_url":"https://YOUR_PUBLIC_RPC","chain_id":"1","factories":{"0x000000000004444c5dc75cb358380d2e3de08a90":"uniswap_v4"},"confirmations":20}]
AITOS_CFEI_DEX_POOLS=[{"rpc_url":"https://YOUR_PUBLIC_RPC","chain_id":"1","pools":{"0x000000000004444c5dc75cb358380d2e3de08a90":"uniswap_v4"},"v4_pool_tokens":{"0xPOOL_ID_32_BYTES":{"token0":"0x0000000000000000000000000000000000000000","token1":"0xTOKEN_ADDRESS"}},"confirmations":20}]
```

Replace the example manager with the canonical deployment for the selected
chain and use the exact bytes32 PoolId emitted by Initialize. token0 and token1
must be strictly address-sorted as in the PoolKey; the zero address represents
the native currency. A pool missing from v4_pool_tokens is intentionally
ignored by swap decoding. PoolManager sender is a caller or router identity,
not proof of the ultimate trader. Dynamic hooks, hook-adjusted deltas and
protocol-specific accounting require separate validation; the observer
records the PoolManager emitted BalanceDelta and fee as evidence.


Example operator-maintained address classification (replace all example
values and retain the provenance ticket/source):

```dotenv
AITOS_CFEI_ENTITY_LABELS=[{"chain":"1","address":"0xYOUR_REVIEWED_ADDRESS","label":"Exchange hot wallet","category":"exchange","source":"internal-review-ticket-123","verified":true,"valid_from":"2026-11-18T00:00:00Z","valid_until":"2027-11-18T00:00:00Z"}]
```
