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

## Crash/replay contract

For every stream batch the worker performs:

1. Read after the durable per-stream Redis ID.
2. Append the batch to the archive file.
3. `flush()` and `fsync()` the archive file.
4. Atomically replace the cursor checkpoint containing the Redis ID and file byte offset.
5. Only then allow the hot stream to be trimmed.

On restart, the worker truncates any bytes after the last durable checkpoint before
reading more entries. This removes the duplicate-record window caused by a crash
after archive write but before cursor checkpoint. Stream IDs are preserved in every
archive record.

Redis Stream IDs are compared numerically as `(milliseconds, sequence)` pairs;
plain string comparison is intentionally not used.

## Existing installations

If an existing installation still has Redis persistence files directly under
`/mnt/aitos-data/redis/`, stop Redis first and run:

```sh
sh scripts/migrate_redis_data_layout.sh
```

The helper refuses to run while Redis is reachable, never overwrites an existing
`live/` path, and never touches `redis/archive/`. Verify the files before starting
Redis again.

The migration is intentionally not automatic because moving a live AOF/RDB file
while Redis is running can corrupt persistence.
