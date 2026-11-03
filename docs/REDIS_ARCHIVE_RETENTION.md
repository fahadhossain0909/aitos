# Redis archive and retention

Production layout:

```text
/mnt/aitos-data/
├── clickhouse/
├── neo4j/
└── redis/
    ├── live/       # Redis /data: AOF/RDB and live working set
    └── archive/    # Historical Redis Stream records + durable cursors
```

The archive worker writes a batch to the archive file and calls `fsync()` before
advancing the per-stream cursor. Cursor updates use an atomic replace. A crash
can therefore replay a batch, but cannot advance the cursor before the batch is
on durable storage.

Redis trimming is allowed only after the archive cursor has passed the oldest
entry eligible for eviction. The worker never deletes an entry first and then
tries to archive it.

Every known stream family has an explicit hot-memory bound; unknown `stream:*`
keys use the conservative default of 5,000 entries. DLQ is capped at 25,000.

Archive files are not Redis persistence files and must not be placed in
`redis/live`. Historical archives are retained on the same persistent data disk
under `redis/archive`.
