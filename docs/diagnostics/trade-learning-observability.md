# Trade / Learning Observability

This diagnostic contract separates raw trade ingestion, order-flow emission, decision generation, and learning persistence so production audits can distinguish an idle strategy from a broken pipeline.

## Required counters

- `trade_events_received`: valid aggTrade events accepted by the market-data ingestion layer.
- `trade_parse_errors`: aggTrade messages rejected during parsing.
- `orderflow_events_emitted`: order-flow events successfully emitted downstream.
- `last_trade_event_time`: timestamp of the most recently accepted trade event.
- `decision_events_received`: journal decision events received by the learning recorder.
- `outcome_events_received`: journal outcome events received by the learning recorder.
- `records_written`: learning experiences persisted successfully.

## Interpretation

`trade_events_received = 0` indicates a trade-stream/adapter problem; non-zero trade events with zero order-flow events indicates an ingestion-to-orderflow problem. Zero decision events can be a valid consequence of zero strategy candidates. Non-zero decision/outcome events with zero records written indicates a learning recorder or persistence problem.

Do not manufacture synthetic learning experiences or alter strategy thresholds merely to make these counters non-zero.
