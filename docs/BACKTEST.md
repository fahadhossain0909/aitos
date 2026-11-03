# ProjectAlpha VPS Backtesting

ProjectAlpha has one canonical backtesting stack with two execution modes:

- **Canonical price-event mode**: deterministic `BacktestEngine` for lightweight OHLCV/trade studies.
- **Full ProjectAlpha market mode**: `ProjectAlphaHistoricalRunner` for trade + L2 replay, shared order-flow/footprint/liquidity/auction intelligence, L2 execution, passive queue simulation, and perpetual margin checks.

Both modes are read-only with respect to historical market data. Both can persist summarized learning experiences when explicitly requested by their integration layer.

## 1. Preferred data source: ClickHouse

ClickHouse is the long-lived historical source. Paper/live ingestion should persist the market history there so future backtests do not require downloading the same period again.

For the full market replay, the source reads:

- `trade_ticks`
- `order_book_snapshots`

For the lightweight engine it can additionally read `market_ohlcv`.

All queries are bounded by symbol/time range and are read-only.

## 2. Full L2/futures replay from ClickHouse

Run the full ProjectAlpha engine directly against the historical data already stored in ClickHouse:

```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.rich_cli \
  --source clickhouse \
  --symbol BTCUSDT \
  --tick-size 0.10 \
  --start 2026-01-01T00:00:00Z \
  --end 2026-02-01T00:00:00Z \
  --initial-cash 10000 \
  --fee-rate 0.0004 \
  --slippage-bps 1 \
  --leverage 1
```

Use a custom decision module with the same contract:

```python
def strategy(state) -> HistoricalDecision:
    ...
```

and pass it with `--decision-strategy package.module:function`.

The CLI returns JSON including decisions, fills, requested/filled quantity, final equity, return, fees, funding, liquidation status, and passive-order statistics.

## 3. Lightweight deterministic replay

For generic timestamp/price datasets:

```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.cli \
  --data /data/events.jsonl \
  --strategy aitos.backtest.cli:buy_and_hold \
  --initial-cash 10000 \
  --fee-rate 0.0004
```

Parquet is supported as well:

```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.cli \
  --data /data/events.parquet \
  --format parquet \
  --strategy aitos.backtest.cli:buy_and_hold
```

## 4. Full market replay from files

If ClickHouse does not yet contain the requested period, the full runner can consume JSONL/Parquet rows with `event_type=trade` or `event_type=orderbook`:

```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.rich_cli \
  --source file \
  --data /data/market-events.parquet \
  --format parquet \
  --symbol BTCUSDT \
  --tick-size 0.10
```

This is a fallback/import path, not the normal long-term storage strategy.

## 5. Learning and evaluation

A backtest is an experiment, not an automatic production deployment. Candidate strategy/model changes must go through the canonical validation path:

```text
Candidate
  -> canonical backtest
  -> walk-forward validation
  -> locked holdout
  -> paper/shadow validation
  -> governance gate
  -> champion model/strategy
```

Historical backtest results can be persisted into the shared Experience Store so the continual-learning layer can use them alongside paper/live experience. Production promotion is intentionally not automatic.

## 6. Paper-trading safety

The backtest service is an opt-in Compose profile and is separate from `aitos-paper`. It has no public network port, mounts file data read-only, and is resource-limited.

Paper trading can remain running while a backtest executes:

```text
Paper trading:  RUNNING  -------------------------------->
Backtest:                 START ---- RUN ---- END
```

Do not use `docker compose down -v` just to run a backtest; persistent database/model volumes must be preserved.

## 7. Operational commands

Check services:

```bash
docker compose ps
docker compose logs -f aitos-paper
docker compose logs -f aitos-learning
```

Run the learning worker explicitly if needed:

```bash
docker compose up -d aitos-learning
```

Stop only paper trading without deleting data:

```bash
docker compose stop aitos-paper
```

Start it again:

```bash
docker compose start aitos-paper
```

Never use `down -v` for routine stop/restart operations.
