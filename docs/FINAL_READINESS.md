# ProjectAlpha Final Readiness

## Architecture contract

ProjectAlpha is designed as one connected learning system, not three isolated bots.

```text
Historical / Paper / Live market data
              |
              v
          ClickHouse
              |
      +-------+--------+
      |                |
   Backtest         Experience
      |                |
      +-------+--------+
              |
      Continual Learning
       |       |       |
      RL   Evolution  Other Models
       \       |       /
        Candidate Model
              |
      Canonical Validation
       |       |       |
   Backtest WalkForward Holdout
              |
         Paper/Shadow
              |
        Governance Gate
              |
           Champion
              |
        New Experience
              +-----------> loop
```

## What is now implemented

- Canonical deterministic `BacktestEngine`.
- Full `ProjectAlphaHistoricalRunner` with shared order-flow, footprint, liquidity and auction intelligence.
- L2 execution, passive queue lifecycle, and perpetual-margin/liquidation simulation.
- ClickHouse historical replay for OHLCV/trades/order-book data.
- Unified ClickHouse trade + L2 market-event source for the full runner.
- Backtest CLI and full L2/futures replay CLI.
- Backtest outcome persistence into the shared Experience Store.
- Paper/live experience persistence and continual-learning worker.
- Durable shared model checkpoints with concurrent-write protection.
- Evolution candidate validation with canonical backtesting, walk-forward and locked holdout gates.
- Candidate/champion model lifecycle without automatic production promotion.
- Backtest isolation from the always-on paper service through the Compose `backtest` profile.
- Persistent ClickHouse/Redis/Neo4j/model volumes.
- Database services bound to localhost in Compose rather than exposed on all interfaces.
- CI, CD, backtest workflow, and production-audit workflows.
- Operational documentation for safe stop/start and backtest execution.

## Intentional safety boundaries

Learning is continuous; production deployment is not autonomous. A learned candidate must pass historical and paper/shadow validation before governance can promote it.

Backtests never mutate market history. Historical file mounts are read-only. The backtest service has no public port and has explicit CPU/memory limits.

## VPS deployment rule

Do not manually run individual application processes before the CD workflow deploys the complete Compose stack. After the final merge, use the repository's existing CD workflow so all services and migrations start from the same revision.

First deployment checks:

```bash
docker compose ps
docker compose logs --tail=200 aitos-paper
docker compose logs --tail=200 aitos-learning
docker compose exec clickhouse clickhouse-client --query 'SELECT 1'
```

Then verify the learning loop:

```text
paper decision -> experience -> ClickHouse -> learning worker -> checkpoint
```

and the replay loop:

```text
ClickHouse market history -> rich_cli -> ProjectAlphaHistoricalRunner -> result
```

Do not delete persistent volumes during routine troubleshooting. In particular, avoid `docker compose down -v` unless data destruction is explicitly intended.
