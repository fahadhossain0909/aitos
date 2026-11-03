# AITOS Architecture Map

## Document status
This is an initial source-backed map, not a claim that every runtime edge has been verified. Last repository baseline inspected for this document: `main` at `b9d4a60a5ac1989455837fec277e2977d520de5e` (2026-11-03). Revalidate after code changes.

## Product boundary
README describes AITOS as an event-driven Binance USDT-M Futures system combining market-data ingestion, Redis Streams, ClickHouse persistence, optional Neo4j, risk controls, opportunity scanning, paper trading, guarded live execution, journaling/XAI, continual learning, historical replay/backtesting, health/metrics, and Docker deployment.

## High-level logical flow
```text
Binance REST / WebSocket
        |
        v
Market-data adapters and ingestion
        |
        v
Decode / normalize / validate
        |
        v
Redis Streams / Event Bus
        |
        +-------------------+-------------------+
        |                   |                   |
        v                   v                   v
Risk and state        Opportunity scanner   Persistence /
consumers              and intelligence      journal / learning
        |                   |
        +---------+---------+
                  v
           Decision / risk gate
                  |
                  v
             Trade lifecycle
              /          \
             v            v
       Paper execution   Guarded live execution

ClickHouse: market history, journals, learning/replay data (verify actual tables/use).
Neo4j: optional knowledge graph (verify enabled paths).
Health/metrics: README documents paper health endpoint on port 8090; verify active configuration.
```

## Architecture boundaries to verify in source/runtime
1. Exchange adapters: subscription setup, endpoint selection, timestamps, reconnect and REST fallback.
2. Ingestion: decoding, normalization, validation, freshness, duplicate/out-of-order handling.
3. Event transport: Redis stream names, consumer groups, offsets, ACK, retry, DLQ and backpressure.
4. State/persistence: source of truth for market state, candidates, orders, positions and journals.
5. Intelligence: features, market structure, liquidity/order-flow, candidate rotation and scoring.
6. Risk: pre-trade validation, exposure, sizing, circuit breaker and mode-specific controls.
7. Execution: paper simulation vs testnet/live adapter; idempotency and fill handling.
8. Lifecycle: persistence, restart recovery, monitoring subscriptions, partial exits, close confirmation.
9. Operations: startup/shutdown, health, metrics, logs, tracing, Docker and CI/CD.

## Entrypoints documented by README
- `python3 run_paper_trading.py` — public market data and simulated orders.
- `python3 run_live_trading.py` — guarded real/testnet execution; do not invoke without explicit authorization.
- `python3 run_continual_learning.py` — learning worker.
- `python3 -m aitos.backtest.cli` — historical backtest.
- `python3 -m aitos.backtest.rich_cli` — richer L2/futures replay.

## Runtime contract
Static architecture does not establish that a component is active. For each boundary, record process identity, readiness, event input/output evidence, error state, and runtime timestamps in the verification report.

## Unknowns requiring source/runtime mapping
- Exact current module graph and initialization order.
- Exact Redis stream names, consumer groups, and DLQ routing.
- Which ClickHouse tables are actively written by each worker.
- Which optional Neo4j paths are enabled in paper mode.
- Actual health/metrics bind host and port for the selected environment.
- Exact Prometheus scrape and Grafana dashboard wiring.
- Which trace/correlation identifiers cross every stage.
- Runtime coverage of candidate/entry/exit and position-recovery telemetry.
