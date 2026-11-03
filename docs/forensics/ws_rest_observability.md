# WS/REST Market-Data Forensics

## Purpose

Trace stale trade creation without changing market-data semantics. This instrumentation is observational: it must not drop, reorder, acknowledge, or mutate market-data events.

## Required lifecycle timestamps

Record monotonic-clock durations and source timestamps at these boundaries:

1. WebSocket connection opened.
2. Last WebSocket message received.
3. Last accepted WebSocket source timestamp per symbol/stream.
4. Idle watchdog fired.
5. Reconnect started/completed.
6. REST recovery requested: request start/end, requested window, response count.
7. REST recovery response: oldest/newest source timestamp, min/max trade ID, accepted/rejected counts and rejection reasons.
8. First WebSocket message after reconnect.
9. Redis publish start/end and batch size.
10. Consumer receive/ack timestamps and measured lag.

## Invariants

- A stale recovery event must never advance the recovery watermark.
- Trade IDs and source timestamps accepted into a single symbol stream must be monotonic.
- Recovery must be explicitly correlated to the reconnect/idle incident that triggered it.
- Transport telemetry must never perform blocking I/O or expensive aggregation on the WebSocket receive path.

## Diagnostic fields

Every recovery cycle should expose:

- `recovery_cycle_id`
- `symbol`
- `stream`
- `ws_last_message_age_ms`
- `ws_last_source_age_ms`
- `idle_timeout_ms`
- `reconnect_attempt`
- `rest_window_start_ms`
- `rest_window_end_ms`
- `rest_oldest_source_ts_ms`
- `rest_newest_source_ts_ms`
- `rest_min_trade_id`
- `rest_max_trade_id`
- `rest_received_count`
- `rest_accepted_count`
- `rest_rejected_stale_count`
- `rest_rejected_id_regression_count`
- `rest_rejected_timestamp_regression_count`
- `redis_publish_latency_ms`
- `consumer_lag_ms`

## Safety rule

This document defines telemetry requirements only. Any behavioral change must be implemented separately, tested, and promoted through a pull request. No Redis CPU, pool, concurrency, or retention setting should be changed based on this telemetry alone.
