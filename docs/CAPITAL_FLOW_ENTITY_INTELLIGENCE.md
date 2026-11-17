# AITOS Capital Flow & Entity Intelligence (CFEI)

## Scope and safety
CFEI is a read-only, evidence-first research subsystem. It observes public exchange aggregates and explicitly configured public-chain addresses/contracts. It does not access private exchange accounts, deanonymize people, place orders, or alter strategy, risk, sizing, stops or execution. Outputs are descriptive evidence or uncalibrated hypotheses.

## Implemented foundation
- Versioned provider-neutral CapitalEvent, EntityEvidence and EntityLabel contracts with UTC validation, finite numeric bounds, explicit provenance and deterministic deduplication.
- Binance USD-M Futures public top-trader position/account ratios and taker buy/sell ratios. These are cohort aggregates, not individual trader identities or private account positions.
- EVM ERC-20 Transfer logs over HTTPS JSON-RPC, bounded block ranges and configurable confirmation depth. Raw token base units are preserved; decimals and USD values are never guessed.
- Solana finalized address activity through standard JSON-RPC. It does not claim complete token-flow reconstruction or common ownership.
- ClickHouse append-only event storage.
- Transparent accumulation/distribution descriptors require a source-specific flow_sign. Unknown-direction transfers are not counted as buys or sells.

## Operational gates not yet complete
These adapters are library components and are not enabled in the paper runtime. Production use still requires durable per-source cursors, fencing leases, reorg rollback/reconciliation, retry budgets, rate-limit scheduling, health/metrics, replay fixtures and explicit operator configuration. No deployment or live-data validation is implied.

## Data semantics
- Binance top-trader endpoints expose aggregate cohorts only.
- An exchange deposit is not proof of a sale; withdrawal is not proof of accumulation.
- Wallet clustering requires independent evidence; shared custody/exchange infrastructure is not proof of common ownership.
- EVM logs need token decimals, point-in-time prices, bridge/internal-transfer classification and custody labels before USD flow can be calculated.
- Solana address activity is not a normalized token-flow ledger.
- Market impact must use contemporaneous comparable liquidity.
- No prediction accuracy or profitability claim is made.

## Cross-market extension
The generic market/venue/instrument contract can normalize SEC/EDGAR, CFTC, FRED and future institutional datasets. Existing World Intelligence SEC/FRED/news ingestion remains the source foundation; CFEI does not reconstruct confidential broker or bank positions.
