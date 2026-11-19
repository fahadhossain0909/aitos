# AEP-1 Phase 2 — Shadow Market Context and Immutable Evidence

Status: additive code and unit tests added; opt-in only. Not enabled by any application
entrypoint and not a production-readiness certificate.

## 1. Scope and safety boundary

This tranche implements three reusable primitives:

1. a bounded, read-only subscriber from the canonical MarketDataBus to the
   universal market-domain event/feature layer;
2. recursively immutable event/state mappings that retain ordinary JSON object
   and array shapes; and
3. conservative inclusion of reserved-but-not-yet-open capital in the
   CapitalGateway's correlation-risk projection.

No order, strategy, venue adapter, subscription default, persistence schema,
risk-limit value, live configuration or production entrypoint is changed.
The shadow subscriber is not instantiated or started automatically.

## 2. MarketDataShadowConsumer

Implementation: `aitos/markets/shadow_consumer.py`.

- Requires an explicit instrument resolver. It never guesses asset class,
  venue, market type, contract size or settlement metadata.
- Requires an explicit allow-list of market types and subscribes only to
  canonical ticker events for those types.
- Uses stable, live-only consumer groups. It does not replay the retained stream.
- Uses a bounded in-process queue (default 2,048 events), maximum instrument
  count (default 500), dedupe window (default 8,192 event IDs), and event-age
  bound (default 5 seconds).
- On queue saturation it drops the newest event and increments
  `rejected_overload`; it never blocks the market-data publisher or grows an
  unbounded queue.
- Rejects stale/future-skewed, out-of-order, duplicate, unresolved, mismatched
  venue/symbol/market-type and invalid-price inputs.
- Converts events through the pure `market_data_event_to_domain` bridge and
  preserves source event ID, revision, sequence, trace/correlation IDs,
  timestamps and source event family.
- Exposes an immutable counter snapshot and explicit `start` / `stop`
  lifecycle. Normal stop drains the bounded queue; emergency stop discards it.

### Delivery semantics

The subscriber callback enqueues and returns. Depending on transport ACK timing,
the bus may acknowledge an event before the in-process worker has processed it.
A process crash can therefore lose queued shadow events. This is intentionally
at-most-once, lossy analytics—not a durable accounting or execution consumer.
Do not reuse this consumer group for durable history, order decisions, risk
authority or any workflow that requires replay. Such workloads need their own
durable consumer and idempotent sink.

### Activation prerequisite

The repository audit did not establish one authoritative instrument-master
resolver that safely resolves Spot, USD-M Futures, COIN-M Futures and future
non-crypto instruments from every canonical ticker envelope. Therefore this
consumer is deliberately not wired into `run_paper_trading.py`,
`run_live_trading.py`, or a market-data gateway. An integration owner must
supply and test that resolver, symbol aliases, market-type allow-list, group
identity, queue sizing, shutdown ownership and telemetry before enabling a
paper-only shadow instance.

For causal Futures↔Spot lead-lag, use the existing timestamp-bucketed
`FuturesSpotLeadLagAnalyzer` with confidence intervals and its explicit
non-authoritative status. Do not interpret arrival-ordered samples from the
generic `CrossMarketIntelligenceEngine` as causal lead-lag evidence.

## 3. Deep immutability

Implementation: `aitos/core/immutable.py`.

- `FrozenDict` and `FrozenList` recursively freeze nested mappings and lists.
- Both remain dict/list subclasses, preserving JSON encoder object/array shape
  and equality with ordinary dict/list values.
- `__copy__` / `__deepcopy__` preserve immutability through
  `dataclasses.asdict`.
- Applied to canonical market-data event payloads and stage timestamps,
  universal market-domain event payloads and stage timestamps, and
  `GlobalMarketState.features`.
- The existing MarketDataBus wire serializer/deserializer now carries
  `stage_timestamps`; older wire envelopes without that optional field still
  deserialize with an empty mapping.
- Tuples remain tuples; sets become tuples because JSON has no set type.

Compatibility gates: mutation attempts must fail at every nested level;
JSON round-trip must retain the old object/array wire shape; dataclass
serialization must remain deterministic. This does not make arbitrary objects
inside payloads serializable—canonical event payloads remain JSON data.

## 4. In-flight risk projection

Implementation: `CapitalGateway._include_in_flight_reservations`.

Before portfolio correlation protection, active reservations for symbols not
already represented by the supplied open-position risk snapshot are projected
into that snapshot as risk percentages. The reservation side is unknown, so
existing conservative unknown-correlation treatment applies and no hedge
credit is granted. Symbols already present in the live-position snapshot are
not counted twice. The policy is re-evaluated inside the SQLite
`BEGIN IMMEDIATE` reservation transaction before insert, so two durable workers
cannot both approve a correlated risk projection from the same stale snapshot.
The in-memory ledger serializes the same callback under its process-local lock.

The existing atomic SQLite reservation ledger remains the capital-budget
authority. This is an additive conservative projection; it does not create a
second risk engine, change risk limits, or authorize execution. It does not
solve the separate data-model gap where an open position and an additional
same-symbol pending order need independently attributable risk. That requires
a lifecycle-backed pending-order projection and is still an acceptance item.

## 5. Regression coverage

Added tests cover:
- explicit live-only market-type subscriptions and subscription cancellation;
- bounded queue overload and stale-event rejection;
- event dedupe, instrument resolution, market-type mismatch, out-of-order data,
  invalid price and instrument-count bounds;
- nested immutable mappings/lists, JSON serialization and dataclass
  `asdict` compatibility for both event contracts and global state;
- canonical event wire round-trip, including stage timestamps and old-field defaults;
- pending reservation inclusion with conservative unknown side metadata;
- SQLite reservation-policy serialization across independent ledger instances.

CI must pass on the final commit before this tranche is marked verified.
No paper soak, production resolver integration, durable replay guarantee,
exchange calibration, latency percentile or live execution claim is made.

## 6. Runtime integration update — 2026-11-19

The consumer is no longer wholly disconnected from the application composition root: an explicitly supplied `market_shadow_consumer` is now owned by `initialize_all` / `shutdown_all`, and receives the canonical bus exposed by `DataIngestionService.market_data_bus`. This remains opt-in; standard paper/live entrypoints do not construct or start it. The explicit `InstrumentMaster` is immutable and versioned, with exact venue + market-type + symbol resolution and no guessing.

The scanner health contract now exposes actual candidate symbols, and the paper canary validates coverage against those symbols rather than inferring non-BTC candidates from a count. See `engineering/AEP1_PHASE3_RUNTIME_INTEGRATION_ADDENDUM.md` for the full status and remaining gates.
