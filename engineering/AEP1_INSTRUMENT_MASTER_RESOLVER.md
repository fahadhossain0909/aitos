# AEP-1 — Authoritative Instrument Master Resolver

Status: implemented as an immutable, explicit registry primitive; not yet wired
into a runner or activated as a live/paper subscription.

## Contract

- Identity is exactly (venue, market_type, symbol), normalized only for case
  and surrounding whitespace.
- Asset class, venue capabilities, contract size, settlement, tick/lot sizes,
  and aliases must be supplied by the authoritative instrument-master source.
- Duplicate normalized identities and duplicate canonical instrument IDs fail
  construction; there is no last-write-wins behavior.
- Missing or unknown identities resolve to no instrument. No symbol-only,
  cross-venue, Spot/Futures, or alias fallback is permitted.
- A registry is an immutable versioned snapshot. Update by building and
  explicitly swapping a new snapshot, not mutating a resolver while consumers
  are running.
- The callable interface is directly compatible with
  MarketDataShadowConsumer(instrument_resolver=master).

## Activation boundary

This primitive does not create an authoritative exchange instrument master,
fetch metadata, or own application lifecycle. It is safe to use only after
an upstream owner has validated and versioned the full Instrument records.
Paper wiring remains opt-in and requires a dedicated consumer lifecycle,
bounded queue sizing, shutdown ownership, health metrics, and paper soak.
It must never be used as the source of truth for financial positions or orders.
