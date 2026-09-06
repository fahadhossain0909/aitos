# AITOS Storage Layout v1

The market-data architecture separates persistent storage by workload. The boot disk is for the operating system, Docker runtime, source checkout, deployment metadata, and small transient files. The dedicated AITOS data disk is the only home for durable trading data.

## Data disk

Default mount: `/mnt/aitos-data`

```text
/mnt/aitos-data/
├── databases/
│   ├── clickhouse/       # durable market history, journals, learning data
│   └── neo4j/            # optional knowledge graph
├── eventbus/
│   └── redis/
│       ├── live/         # Redis AOF/RDB hot event-bus state
│       └── archive/      # append-only Redis stream archive
├── research/
│   ├── backtest/         # large backtest datasets and generated replay inputs
│   └── replay/           # order-book/trade replay material
├── artifacts/
│   ├── backups/          # explicit database/application backups
│   └── snapshots/        # point-in-time research/state snapshots
└── runtime/
    ├── models/           # durable model/continual-learning state
    └── tmp/              # disposable data-disk temporary workspace
```

During the migration, `clickhouse`, `neo4j`, and `redis` at the data-disk root are compatibility symlinks to the canonical directories. They contain no second copy of the data. New code should use the canonical paths.

## Boot disk

The boot disk should remain intentionally boring and bounded:

```text
/opt-or-home/aitos/       # deployment checkout (currently $HOME/aitos)
Docker/containerd state   # /var/lib/docker and /var/lib/containerd
OS/package/journal state  # normal Debian system paths
small AITOS runtime logs  # bounded container/system logs
```

Large market history, Redis archives, replay/backtest datasets, backups, snapshots, and model state must not silently fall back to the boot disk.

## Storage rules

1. `AITOS_DATA_ROOT` must be an absolute mounted data-disk path.
2. `bootstrap_storage.sh` verifies the configured filesystem UUID before creating data directories.
3. If a legacy path is a real directory, bootstrap fails closed rather than hiding it behind a symlink.
4. Redis hot state and its archive are physically separated from ClickHouse data.
5. Backtest/replay data is separated from operational backups/snapshots.
6. The VPS hard-reset action may delete the AITOS data tree, but it must never delete the data-disk mount itself or touch unrelated host filesystems.
7. The application should treat ClickHouse as the durable canonical market-history store; Redis is the live event bus/hot state, not the long-term database.

## Migration intent

The current deployment still exposes a few legacy environment variable names such as `CLICKHOUSE_DATA_DIR`, `REDIS_DATA_DIR`, and `NEO4J_DATA_DIR`. The bootstrap compatibility links allow the new physical layout to be introduced without duplicating existing bytes. As the canonical deployment environment is migrated, those variables should point directly at `databases/...` and `eventbus/...` paths and the compatibility links can eventually be removed.
