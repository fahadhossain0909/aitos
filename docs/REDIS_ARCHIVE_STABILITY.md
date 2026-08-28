# Redis archive stability

Production layout:

```text
/mnt/aitos-data/
├── clickhouse/
├── neo4j/
└── redis/
    ├── live/       # Redis AOF/RDB and hot working set
    └── archive/    # historical Redis Stream archive + cursors
```

For each stream batch, the worker reads after the durable Redis Stream ID,
appends to the archive, flushes and fsyncs the archive, then atomically replaces
the cursor containing the stream ID and byte offset. Only after that checkpoint
is the stream eligible for trimming.

On restart, bytes after the last durable offset are truncated before more data is
appended. This makes archive replay idempotent across a crash between archive
fsync and cursor checkpoint. Redis Stream IDs are compared numerically as
(milliseconds, sequence), never as plain strings.

## Existing installations

If Redis persistence files are still directly under `/mnt/aitos-data/redis/`, stop
Redis first and run:

```sh
sh scripts/migrate_redis_data_layout.sh
```

The helper refuses to run while Redis is reachable, never overwrites `live/`, and
never touches `archive/`. Verify the moved files before starting Redis again.
