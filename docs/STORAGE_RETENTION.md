# AITOS storage and retention policy

## Goals

AITOS uses bounded retention so storage growth cannot silently consume the filesystem needed by production and deployment.

- ClickHouse is the primary historical source for market data retained from paper/live operation.
- Backtest downloads are a bounded, re-downloadable cache.
- Trade/decision/risk/model/experience data is protected from automatic deletion.
- High-volume market history such as L2/order-book data is evictable and can be downloaded again when absent.
- Boot-disk application data under `.storage/others` is capped at **22.5 GiB**.
- The default ClickHouse storage budget is **130 GiB**, with a normal cleanup target of **120 GiB**.
- The 150-GiB data disk keeps at least **20 GiB free** as an emergency filesystem headroom target; normal operation aims for **25 GiB free**.

## Boot-disk automatic cleanup

The storage-maintenance service checks the boot-disk application-data tree every **5 minutes**. When `.storage/others` exceeds 22.5 GiB, it removes the **oldest disposable files first**.

The cleanup batch is normally **2.5% of the managed dataset**. If a large write has already pushed the tree beyond the limit, the cleanup removes at least the amount required to return under the 22.5-GiB budget, rather than waiting for the next deployment or failing deployment.

This is not a full wipe. Newer files are retained; only the oldest files are selected. The disposable areas are intended for re-downloadable caches, backups and snapshots.

## Data-disk emergency protection

ClickHouse, Neo4j and Redis live on `/mnt/aitos-data`; they are **not** subject to the boot-disk file-pruner.

The maintenance service also checks the data-disk filesystem every 5 minutes. If free space falls below **20 GiB**, it enters emergency ClickHouse retention mode and immediately requests the shortest configured historical window (**7 days**) for configured evictable ClickHouse tables.

Redis uses AOF rewrite thresholds so deleted/expired keys do not leave an unnecessarily fragmented append-only file. Neo4j is configured to retain transaction logs for a bounded window (`2 days 100M`), allowing Neo4j itself to rotate obsolete transaction logs.

**Important safety boundary:** arbitrary files inside ClickHouse, Neo4j or Redis database directories are never deleted by a generic filesystem `rm`. Database files are internally managed and deleting them by age can corrupt the database or destroy required state. Only database-native retention/eviction mechanisms are used.

If the data disk is filled primarily by protected Neo4j graph data, Redis live state, or protected ClickHouse tables, there is no universally safe generic deletion rule. Those datasets require a domain-specific retention policy before automatic deletion can be enabled.

## Retention ladder

The controller selects from:

`90 days → 30 days → 15 days → 10 days → 7 days`

It shortens retention when the configured ClickHouse target is exceeded. During a data-disk emergency it immediately uses the 7-day emergency window.

## Protected vs evictable ClickHouse data

The controller never automatically deletes protected trading/learning records such as:

- trades / trade ticks
- orders and fills
- positions / portfolio state
- decisions and journals
- risk records
- model versions / model outputs
- experience/replay data
- strategy/execution records

Currently eligible for automatic historical eviction:

- `order_book_snapshots`
- `order_book_updates` (when present)
- `market_ohlcv`

Unknown tables are **not** deleted automatically. A new evictable table must be deliberately added to `aitos/storage/maintenance.py` with its event-time column.

## Backtest download cache

The cache is mounted at `/data/backtest` and capped at 20 GiB by default. When it exceeds the cap, the oldest eligible files are removed first. It is disposable: missing historical partitions can be downloaded again later.

Downloaded Parquet files are consumed directly by the backtest engine and are not ingested into ClickHouse.

## Operational rule

The desired failure mode is **degrade retention, not stop production**:

1. protect the filesystem headroom;
2. remove oldest disposable boot-disk files when their budget is exceeded;
3. shorten ClickHouse historical retention when the data disk approaches its emergency free-space threshold;
4. let Redis compact its AOF and Neo4j rotate old transaction logs;
5. never `rm -rf` database directories as a generic emergency action.

For a non-destructive maintenance run, set:

```text
STORAGE_MAINTENANCE_DRY_RUN=true
```

Production uses `false` and runs every five minutes.
