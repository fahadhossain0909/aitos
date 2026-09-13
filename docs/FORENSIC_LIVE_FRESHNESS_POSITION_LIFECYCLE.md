# AITOS Live Freshness + Position Lifecycle Forensics

## Purpose

This forensic pass establishes one timestamped causal chain across market-data transport, Redis/event delivery, scanner source selection, position monitoring, exit intelligence, execution, and reconciliation.

It is diagnostic-only: it must not change trading decisions, reconnect policy, REST fallback policy, or exit thresholds.

## Core invariant

For every live symbol and every open position we must be able to answer:

`exchange/source -> WS receive -> parse -> Redis publish -> Redis consume -> feature state -> scanner/strategy -> position update_price -> exit evaluation -> exit submission -> fill/close -> reconciliation`

The first missing or stale edge is the root-cause candidate. Later symptoms must not be labeled as independent failures until that edge is explained.

## Required event fields

Every forensic event should include, where applicable:

- `forensic_trace_id`
- `symbol`
- `event_type`
- `source` (`ws`, `rest_bootstrap`, `rest_fallback`, `redis`, `derived`)
- `source_event_ts`
- `received_ts`
- `published_ts`
- `consumed_ts`
- `processed_ts`
- `freshness_ms`
- `sequence` or exchange update id when available
- `topic`
- `connection_id` / stream id when available
- `position_id` when applicable
- `trade_id` when applicable

Timestamps must be UTC and monotonic-duration calculations must use a monotonic clock where possible.

## Market-data state machine

Do not equate event silence with transport failure. Record these states independently:

- `WS_CONNECTED`
- `WS_READER_ALIVE`
- `WS_EVENT_FLOWING`
- `WS_STALE`
- `WS_RECONNECTING`
- `WS_TRANSPORT_ERROR`
- `REST_BOOTSTRAP`
- `REST_EMERGENCY_FALLBACK`
- `WS_RECOVERED`

A reconnect may only be classified as transport-driven when transport/reader evidence exists. A data-staleness event without transport evidence is `WS_STALE`, not `WS_TRANSPORT_ERROR`.

## Scanner evidence

For every scan cycle/candidate record:

- market-data source
- orderbook age
- last WS event timestamp
- last REST event timestamp
- fallback reason
- whether the candidate was accepted while on REST fallback
- decision timestamp

REST fallback must remain explicitly distinguishable from live WS data.

## Position heartbeat

For every open position, periodically record:

- `position_opened_ts`
- `last_market_event_ts`
- `last_update_price_ts`
- `market_age_ms`
- `update_price_age_ms`
- `last_exit_evaluation_ts`
- `exit_evaluation_count`
- exit tier/score/reasons/action
- current price and PnL
- market-context source

If an open position has no `update_price` for the configured forensic threshold, emit `POSITION_MARKET_FEED_STALE`. This must be distinguishable from `HOLD`.

## Exit evidence

Each exit evaluation should identify whether it actually ran. Never infer an exit-engine `HOLD` from the absence of an exit event.

Required distinction:

1. `EXIT_EVALUATED_HOLD`
2. `EXIT_EVALUATED_CANDIDATE`
3. `EXIT_SUBMITTED`
4. `EXIT_FILLED`
5. `POSITION_CLOSED`
6. `POSITION_MARKET_FEED_STALE`

## First-divergence analysis

The forensic report must calculate the first failed edge for each incident:

1. WS connection -> raw event
2. raw event -> parsed event
3. parsed event -> Redis publish
4. Redis publish -> Redis consume
5. Redis consume -> live state update
6. live state -> scanner/strategy
7. market event -> `TradeLifecycle.update_price`
8. `update_price` -> exit evaluation
9. exit evaluation -> execution
10. execution -> close/reconciliation

Do not call an incident a scanner bug if the first divergence is upstream market-data delivery. Do not call a position an exit-intelligence failure if no market heartbeat reached `update_price`.

## Acceptance criteria

The forensic workflow is successful only when one run can answer:

- Why did a websocket reconnect?
- Was the socket actually dead or only the data path stale?
- Why did a symbol enter REST fallback?
- How old was the data at the scanner decision?
- For every open position, when was the last market update?
- Did `update_price` execute?
- Did Exit Intelligence evaluate?
- If it evaluated, what did it decide and why?
- If it requested an exit, did execution/fill/reconciliation complete?

## Safety

This document defines observability only. Do not use forensic telemetry to silently alter strategy behavior. Any production behavior change must be a separate reviewed change.