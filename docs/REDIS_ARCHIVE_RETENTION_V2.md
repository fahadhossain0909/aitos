# Redis archive v2 safety contract

The Redis archive worker follows this ordering for every stream batch:

1. Read entries after the durable per-stream cursor.
2. Append the complete batch to the archive on the persistent data disk.
3. Flush and fsync the archive file.
4. Atomically replace the cursor checkpoint.
5. Only after the cursor has crossed the retention boundary may the hot Redis stream be trimmed.

A crash before step 4 can replay a batch; it cannot make the worker believe an
undurable batch was archived. Archive records retain the Redis Stream ID so
replay/deduplication remains deterministic.

Storage layout:

```text
/mnt/aitos-data/
├── clickhouse/
├── neo4j/
└── redis/
    ├── live/       # Redis /data
    └── archive/    # historical stream archive + cursors
```
