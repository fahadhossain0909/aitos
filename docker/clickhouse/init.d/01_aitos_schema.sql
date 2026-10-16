-- AITOS ClickHouse schema initialization.
-- These tables are created automatically when the ClickHouse container
-- starts for the first time with an empty data volume.
-- Mirrors the DDL in aitos/data/repository.py.

CREATE DATABASE IF NOT EXISTS aitos;

CREATE TABLE IF NOT EXISTS aitos.market_ohlcv (
    time DateTime64(3, 'UTC'), symbol String, timeframe String,
    open Float64, high Float64, low Float64, close Float64,
    volume Float64, quote_volume Float64, trades_count UInt32,
    taker_buy_volume Float64, taker_buy_quote_volume Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, timeframe, time);

CREATE TABLE IF NOT EXISTS aitos.order_book_snapshots (
    time DateTime64(3, 'UTC'), symbol String, bid_levels String, ask_levels String,
    spread Float64, depth_ratio Float64, last_update_id UInt64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time);

CREATE TABLE IF NOT EXISTS aitos.trade_ticks (
    time DateTime64(3, 'UTC'), symbol String, trade_id UInt64, price Float64,
    quantity Float64, side String, is_buyer_maker UInt8
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time);

CREATE TABLE IF NOT EXISTS aitos.funding_rates (
    time DateTime64(3, 'UTC'), symbol String, funding_rate Float64, mark_price Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time);

CREATE TABLE IF NOT EXISTS aitos.open_interest (
    time DateTime64(3, 'UTC'), symbol String, open_interest Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time) ORDER BY (symbol, time);

CREATE TABLE IF NOT EXISTS aitos.market_events (
    event_time DateTime64(3, 'UTC'), ingest_time DateTime64(3, 'UTC'),
    event_id String, exchange LowCardinality(String), market LowCardinality(String),
    symbol LowCardinality(String), event_type LowCardinality(String),
    source LowCardinality(String), sequence Nullable(UInt64), schema_version UInt16,
    correlation_id String, trace_id String, payload_json String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_time)
ORDER BY (symbol, event_type, event_time, event_id);

CREATE TABLE IF NOT EXISTS aitos.live_analytics_events (
    event_time DateTime64(3, 'UTC'), ingest_time DateTime64(3, 'UTC'),
    event_id String, category LowCardinality(String), exchange LowCardinality(String),
    market LowCardinality(String), symbol LowCardinality(String),
    source_module LowCardinality(String), correlation_id String,
    schema_version String, payload_json String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(event_time)
ORDER BY (category, symbol, event_time, event_id);

CREATE TABLE IF NOT EXISTS aitos.learning_experiences (
    experience_id UUID, timestamp DateTime64(3, 'UTC'), source LowCardinality(String),
    symbol LowCardinality(String), decision LowCardinality(String), outcome Nullable(String),
    reward Float64, confidence Float64, quantity Float64, price Nullable(Float64),
    features_json String, market_state_json String, risk_state_json String,
    strategy_version String, model_version String, metadata_json String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp, experience_id);

CREATE TABLE IF NOT EXISTS aitos.trade_runtime_state (
    trade_id String, symbol LowCardinality(String), state LowCardinality(String),
    payload_json String, updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY trade_id;

CREATE TABLE IF NOT EXISTS aitos.portfolio_drawdown_state (
    asset LowCardinality(String), time DateTime64(3, 'UTC'), equity_usd Float64,
    peak_equity_usd Float64, drawdown_pct Float64
) ENGINE = ReplacingMergeTree(time) ORDER BY asset;
