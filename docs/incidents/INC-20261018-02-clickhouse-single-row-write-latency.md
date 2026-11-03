# INC-20261018-02 — Recurring WARNING: ClickHouse single-row book-snapshot writes 100–300ms

- **Severity:** P2-low (no data loss, no backlog; noisy telemetry + inefficient inserts)
- **Status:** INVESTIGATING — root cause identified, remediation prepared, awaiting approval (Level 3: persistence behavior)
- **First observed:** 2026-10-18T13:21Z (first minutes after stack start)
- **Last observed:** ongoing while stack runs
- **Affected:** `aitos/market_data/persistence_sink.py`, ClickHouse persistence path (paper mode; presumably any mode)

## Evidence

Log signature (repeats ~1/s per symbol, e.g. BTCUSDT):

```
WARNING aitos.forensics.contention "clickhouse batch write latency"
  operation=save_order_book_snapshots latency_ms=117–305 rows=1
WARNING aitos.forensics.root_cause "persistence batch write latency"
  stage=persistence_batch_write batch_size=1 event_types={book_snapshot:1}
  write_latency_ms=119–305 queue_depth=0
```

Health endpoint: healthy; persistence errors: 0; queue depth always 0.

## Confirmed facts (static + runtime)

1. Book snapshots are per-symbol throttled (`_book_interval`, `persistence_sink.py:124-131`) → events arrive slower than the sink's 50ms batch window (`batch_wait_seconds=0.05`, `batch_size=100`).
2. Therefore every batch contains exactly 1 book row (`batch_size: 1`) despite the batching layer.
3. Each single-row ClickHouse insert takes 100–300ms on this host (Docker Desktop, named volume).
4. Forensic telemetry flags >=100ms per write by design (`runtime_contention_telemetry.py`, `root_cause_telemetry.py:44`).
5. No backlog, no drops, no errors — persistence keeps up with the throttled rate.

## Root cause

Sparse (throttled) book-snapshot arrival defeats the 50ms micro-batching window; each write is a single-row insert whose latency exceeds the 100ms forensic threshold. The threshold and the insertion pattern are mismatched for this host's disk characteristics.

## Remediation options (NOT applied — Level 3 approval required)

- **A (recommended):** dedicated time-windowed buffer for BOOK_SNAPSHOT persistence (flush every ~5s or N rows). Historical-only data; up to 5s extra persistence latency is acceptable. Requires regression check on shutdown flush.
- **B:** raise the >100ms warning threshold for `save_order_book_snapshots` to ~500ms (cosmetic; keeps current write pattern).
- **C:** no change — warnings are by-design forensic signal; harmless but noisy.

## Tests / rollback

N/A until an option is approved. Rollback for A: revert the sink batching change (single commit).

## Remaining risks

Log noise can mask genuinely slow writes if thresholds are raised blindly (option B/C). Option A keeps the 100ms signal meaningful for other operations.
