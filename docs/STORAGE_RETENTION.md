# AITOS storage and retention policy

## Goals

AITOS uses bounded retention so storage growth cannot silently consume the filesystem needed by production and deployment.

- ClickHouse is the primary historical source for market data retained from paper/live operation.
- Backtest downloads are a bounded, re-downloadable cache.
- Trade/decision/risk/model/experience data is protected from automatic deletion.
- High-volume market history such as L2/order-book data is evictable and can be downloaded again when absent.
- The boot disk is protected by a **7.5 GiB minimum-free-space reserve** rather than a fixed `.storage/others` quota.
- The 150-GiB data disk keeps at least **20 GiB free** as an emergency filesystem headroom target; normal operation aims for **25 GiB free**.

## Boot-disk automatic cleanup

The storage-maintenance service checks boot-disk free space every **5 minutes**. When available boot-disk space falls below the 7.5-GiB reserve, it removes the **oldest files first** from explicitly disposable application directories: `cache`, `caches`, `logs`, `backtest`, `snapshots`, `tmp`, and `backups`.

The cleanup batch is normally **2.5% of the disposable dataset**, but it always removes at least the amount required to restore the configured free-space reserve. Therefore a large write that exhausts the boot disk does not require a deployment failure to recover: the maintenance service can progressively remove old disposable data until the reserve is restored.

This is intentionally **not a full filesystem wipe**. Protected application data and the database disk are outside the generic file-pruner. Only explicitly disposable files are eligible, and the oldest files are selected first.

## Deployment protection

CD performs Docker/container/cache cleanup and checks the boot-disk reserve before and after deployment. If the reserve is breached, it invokes the boot-disk retention guard before continuing. If the reserve still cannot be restored, deployment fails rather than consuming the remaining filesystem headroom.

The deployment must also keep `/mnt/aitos-data` mounted. ClickHouse, Neo4j and Redis remain on that data disk and are never redirected to the boot disk as a fallback.

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

1. protect the boot filesystem headroom;
2. remove oldest disposable boot-disk files when the free-space reserve is breached;
3. shorten ClickHouse historical retention when the data disk approaches its emergency free-space threshold;
4. let Redis compact its AOF and Neo4j rotate old transaction logs;
5. never `rm -rf` database directories as a generic emergency action.

For a non-destructive maintenance run, set:

```text
STORAGE_MAINTENANCE_DRY_RUN=true
```

Production uses `false` and runs every five minutes.
