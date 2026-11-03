-- AITOS ClickHouse schema initialization.
-- These tables are created automatically when the ClickHouse container
-- starts for the first time with an empty data volume.
--
-- Keep this in sync with the DDL declared in the code:
--   aitos/data/repository.py              market data, analytics, learning, trade state
--   aitos/journal/repository.py           trades, journal_entries
--   aitos/journal/decision_repository.py  decision_journal
--   aitos/data/market_os_persistence.py   market_orderflow, market_liquidity_events,
--                                         market_live_state, order_book_events
--   aitos/intelligence/amt/persistence.py amt_sessions
--   aitos/capital/persistence.py          prop_* tables

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

-- ---------------------------------------------------------------------------
-- aitos/journal/repository.py
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aitos.trades (
    recorded_at DateTime64(3, 'UTC'),
    trade_id String,
    symbol String,
    side String,
    entry_price Float64,
    quantity Float64,
    leverage Float64,
    position_size_usd Float64,
    risk_amount_usd Float64,
    strategy_id String,
    sl_price Float64,
    tp_price Float64,
    state String,
    entry_time String,
    exit_price Nullable(Float64),
    exit_time Nullable(String),
    exit_reason Nullable(String),
    pnl Nullable(Float64),
    pnl_percent Nullable(Float64),
    rejection_reason Nullable(String),
    mae_price Nullable(Float64),
    mfe_price Nullable(Float64),
    mae_r Nullable(Float64),
    mfe_r Nullable(Float64),
    payload String
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(recorded_at)
ORDER BY (trade_id, recorded_at);

CREATE TABLE IF NOT EXISTS aitos.journal_entries (
    created_at DateTime64(3, 'UTC'),
    entry_id String,
    trade_id Nullable(String),
    entry_type String,
    market_context String,
    confidence_score Nullable(Float64),
    order_flow_observations String,
    liquidity_observations String,
    amt_observations String,
    lead_lag_observations String,
    mistakes Array(String),
    lessons Array(String),
    improvements Array(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (entry_type, created_at);

-- ---------------------------------------------------------------------------
-- aitos/journal/decision_repository.py
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aitos.decision_journal (
    recorded_at DateTime64(3, 'UTC'), decision_id String, record_type String,
    trade_id Nullable(String), symbol String, side String, strategy_id String, regime String,
    confidence Nullable(Float64), payload String, pnl Nullable(Float64), pnl_percent Nullable(Float64),
    risk_amount_usd Nullable(Float64), r_multiple Nullable(Float64), holding_seconds Nullable(Float64),
    exit_reason Nullable(String),
    evidence_contributions String DEFAULT '[]'
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at) ORDER BY (decision_id, recorded_at);

-- ---------------------------------------------------------------------------
-- aitos/data/market_os_persistence.py
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aitos.market_orderflow (
    time DateTime64(3, 'UTC'), event_id String, symbol LowCardinality(String),
    trade_count UInt64, buy_volume Float64, sell_volume Float64, delta Float64,
    cvd Float64, buy_ratio Float64, aggression Float64, imbalance Float64,
    vwap Float64, last_price Float64, direction String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id);

CREATE TABLE IF NOT EXISTS aitos.market_liquidity_events (
    time DateTime64(3, 'UTC'), event_id String, symbol LowCardinality(String),
    kind LowCardinality(String), side LowCardinality(String), score Float64,
    price Float64, details_json String, last_update_id UInt64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id);

CREATE TABLE IF NOT EXISTS aitos.market_live_state (
    time DateTime64(3, 'UTC'), event_id String, symbol LowCardinality(String),
    trade_count UInt64, order_flow_json String, liquidity_events_json String,
    best_bid Nullable(Float64), best_ask Nullable(Float64),
    state_timestamp Nullable(DateTime64(3, 'UTC'))
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id);

CREATE TABLE IF NOT EXISTS aitos.order_book_events (
    time DateTime64(3, 'UTC'), event_id String, symbol LowCardinality(String),
    bid_levels String, ask_levels String, spread Float64, depth_ratio Float64,
    last_update_id UInt64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(time)
ORDER BY (symbol, time, event_id);

-- ---------------------------------------------------------------------------
-- aitos/intelligence/amt/persistence.py
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aitos.amt_sessions (
    session_id String,
    start_time DateTime64(3, 'UTC'),
    end_time DateTime64(3, 'UTC'),
    payload String
) ENGINE = ReplacingMergeTree(end_time)
ORDER BY (session_id);

-- ---------------------------------------------------------------------------
-- aitos/capital/persistence.py
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS aitos.prop_accounts (
    recorded_at DateTime64(3, 'UTC'), account_id String, provider LowCardinality(String),
    platform LowCardinality(String), account_mode LowCardinality(String), initial_balance Float64,
    currency LowCardinality(String), api_enabled UInt8, automation_enabled UInt8, metadata_json String
) ENGINE = ReplacingMergeTree(recorded_at) ORDER BY (provider, account_id);

CREATE TABLE IF NOT EXISTS aitos.prop_rule_snapshots (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    daily_loss_limit Nullable(Float64), max_drawdown Nullable(Float64),
    daily_loss_uses_equity UInt8, max_drawdown_uses_equity UInt8, api_allowed UInt8,
    automation_allowed UInt8, news_trading_allowed UInt8, overnight_allowed UInt8,
    weekend_allowed UInt8, max_order_notional Nullable(Float64)
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at);

CREATE TABLE IF NOT EXISTS aitos.prop_equity_snapshots (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    balance Float64, equity Float64, day_start_equity Float64, high_water_mark Float64,
    daily_loss Float64, drawdown Float64, realized_pnl_today Float64, unrealized_pnl Float64
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at);

CREATE TABLE IF NOT EXISTS aitos.prop_orders (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    venue LowCardinality(String), client_order_id String, order_id String, symbol LowCardinality(String),
    side LowCardinality(String), order_type LowCardinality(String), quantity Float64,
    reference_price Float64, fill_price Nullable(Float64), filled_quantity Nullable(Float64),
    success UInt8, error Nullable(String), metadata_json String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at, order_id);

CREATE TABLE IF NOT EXISTS aitos.prop_compliance_events (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    venue LowCardinality(String), allowed UInt8, reason String, symbol LowCardinality(String),
    side LowCardinality(String), quantity Float64, reference_price Float64, projected_loss Float64,
    correlation_id String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at, correlation_id);

CREATE TABLE IF NOT EXISTS aitos.prop_payouts (
    recorded_at DateTime64(3, 'UTC'), provider LowCardinality(String), account_id String,
    payout_id String, amount Float64, currency LowCardinality(String), status LowCardinality(String),
    metadata_json String
) ENGINE = MergeTree() PARTITION BY toYYYYMM(recorded_at)
ORDER BY (provider, account_id, recorded_at, payout_id);
