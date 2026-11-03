# Redis Retention and Historical Archive

Redis is the latency-sensitive hot event bus. It must not become the long-term historical store.

## Policy

Every `stream:*` key is archived to the persistent data disk under:

`/mnt/aitos-data/redis-archive/`

The archive worker writes daily JSONL files and persists its per-stream cursors in `.cursors.json`. It only trims Redis after the archive cursor has crossed the eviction boundary. This means historical entries are retained on disk before they are removed from the hot Redis stream.

Hot retention defaults:

| Stream family | Hot entries |
| --- | ---: |
| `market.trade.*` | 25,000 |
| `market.orderbook.*` | 25,000 |
| `market.liquidity.*` | 100,000 |
| `market.live_state.*` | 25,000 |
| `market.orderflow.*` | 25,000 |
| `market.kline.*` | 10,000 |
| `market.opportunity_scanned` | 5,000 |
| `decision.*` | 10,000 |
| `journal.*` | 10,000 |
| `trade.*` | 10,000 |
| `risk.*` | 10,000 |
| `intel.*` | 10,000 |
| `stream:dlq` | 25,000 |
| unknown `stream:*` | 5,000 |

Unknown streams therefore cannot grow indefinitely even if a new producer is added later.

## Safety rules

1. Redis remains the hot working set only.
2. Historical data is written to the persistent data disk before the archive worker trims a stream.
3. The archive worker uses bounded batches so it cannot load an entire stream into RAM.
4. Cursor state is atomically replaced after each archived batch.
5. Existing application consumers are not modified by the archive worker.
6. The storage-maintenance job does not treat the Redis archive as disposable boot-disk data.

## Important operational note

The archive is intentionally append-only. The data disk is finite, so disk-free monitoring must remain enabled. This policy does not silently delete historical Redis archives; any future archive-retention policy must be an explicit, separate decision.
