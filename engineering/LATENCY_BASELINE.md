# AITOS Latency Baseline

## Baseline status: NOT MEASURED
No runtime measurements were collected while preparing this document. Do not treat blank values as zero or infer latency from configuration, source code, or historical reports.

## Measurement record
| Stage | p50 | p95 | p99 | Max | Samples | Window / mode | Status |
|---|---:|---:|---:|---:|---:|---|---|
| Exchange receive -> decode | — | — | — | — | — | — | NOT MEASURED |
| Decode -> validation | — | — | — | — | — | — | NOT MEASURED |
| Validation -> publish | — | — | — | — | — | — | NOT MEASURED |
| Publish -> consumer receive | — | — | — | — | — | — | NOT MEASURED |
| Consumer handler | — | — | — | — | — | — | NOT MEASURED |
| Feature calculation | — | — | — | — | — | — | NOT MEASURED |
| Scanner evaluation | — | — | — | — | — | — | NOT MEASURED |
| Strategy + risk decision | — | — | — | — | — | — | NOT MEASURED |
| Order submit -> acknowledgement | — | — | — | — | — | — | NOT MEASURED |
| Acknowledgement -> fill | — | — | — | — | — | — | NOT MEASURED |
| Fill -> persistence | — | — | — | — | — | — | NOT MEASURED |
| Exit decision -> close confirmation | — | — | — | — | — | — | NOT MEASURED |

## Measurement protocol
1. Record commit SHA, environment, OS, CPU/RAM, service versions, trading mode, symbols/workload, sample count and UTC window.
2. Confirm clock synchronization for cross-host correlation.
3. Prefer monotonic timers for durations inside one process.
4. Separate event processing time from queue residence and network time.
5. Collect histogram observations rather than calculating percentiles from a few samples.
6. Capture throughput, queue depth, oldest message age, event freshness, dropped/invalid/duplicate counts and resource utilization alongside latency.
7. Compare before/after under equivalent workloads.
8. Record warm-up and outage intervals; do not hide tail latency.

## Acceptance
A performance change is accepted only when the targeted metric improves without degrading data correctness, candidate coverage, position monitoring, execution safety, or other pipeline stages.

## Revision-gate investigation — 2026-11-06

No production stage latency/capacity samples collected: p50/p95/p99/max remain
NOT MEASURED for the proposed revision/CAS/outbox path (not implemented).
The embedded evidence suite's 1.54s and backend suite's 55.71s are test elapsed
times, not trading-path measurements or SLO certification. No market feed,
Redis consumer or real account was exercised. Prototype approval must precede
measurement of transaction contention and outbox lag with documented workload.

## Unwired revision prototype — 2026-11-06

Prototype correctness is VERIFIED, not production speed. History validation is
streaming O(history); outbox selection validates scoped histories. Transaction
contention, projection lag and p50/p95/p99/max under a representative workload are
NOT MEASURED. The 39-test 5.69s and backend 62.91s are test elapsed times only.
No risk checks/calculations were removed, no Redis concurrency/pool changed and
no production latency claim is made. Preserve integrity when designing later
benchmarks, bounded operational policy and separately approved runner wiring.

## Measured synthetic typed-paper baseline — 2026-11-06

VERIFIED local-only: Python 3.11.2/SQLite 3.40.1, Linux sandbox, WAL/FULL,
perf_counter_ns, single writer; no warmup subtraction or contention workload.
20 synthetic trades × 5 revisions, 100 append samples: p50 2.142ms, p95 2.668ms,
p99 3.411ms, max 3.938ms. 50 full recovery samples (100 history events, 20 typed
heads plus decoded Trade copies): p50 10.213ms, p95 10.598ms, p99/max 10.966ms.
Reproduce with engineering/tools/benchmark_paper_revision.py; raw ignored artifact
is tmp/paper-binding/benchmark.json. No integrity calculations removed.
Production stage latency, contention/capacity, real-feed/order/sink latency and
soak remain NOT MEASURED; these local values do not establish production SLOs.

## Amendment integrity path — 2026-11-06

No new latency percentile/capacity measurements collected for amendments.
Receipt uniqueness scans scoped retained events; amendment replay validates parent
and derived financial data. Production contention/outbox lag/end-to-end latency
remain NOT MEASURED. Prior local typed-paper microbenchmark is historical and does
not certify this extension. Preserve checks; establish workload/indexing/bounds
and real-service evidence before activating a projector or runner.

## Addendum — projector tranche (2026-11-06)

| Stage | p50 / p95 / p99 / max | Workload window | Status |
|---|---|---|---|
| Journal scan → sink lookup/insert/readback → ACK → API → browser | Not collected | No representative capacity run | NOT MEASURED |

Test durations are regression execution times, not runtime latency evidence.
Batch size 1–100, per-sink-I/O timeout ≤30s and server query/read/result limits
are safety bounds, not achieved SLOs. Local validation remains O(history), with
SQLite work offloaded to threads; no total-attempt CPU deadline. Separate
real-service load/soak and percentile measurement remain required. See
`PROJECTOR_AND_PERSISTENCE_CONSOLE_2026-11-06.md`.

## Paper/native tranche (2026-11-06)

Runtime p50/p95/p99/max, representative workload capacity and real-service soak
window: **NOT MEASURED**. Full regression timings in the completion report are not
latency SLOs. Embedded harness loops are test logic, not remote-service evidence.
SQLite full-history validation and cancellation drain can take unbounded CPU time
relative to the sink per-I/O timeout. See `WINDOWS_PAPER_INTEGRATION_2026-11-06.md`.

## Finishing tranche — 2026-11-06

Production/runtime percentiles and representative capacity remain NOT MEASURED. Regression duration is not a latency SLO; native drain and journal history validation have no certified overall deadline. See `PAPER_NATIVE_HARDENING_2026-11-06.md`.

## Recorded account boundary — 2026-11-06

Provider-boundary runtime latency/capacity percentiles NOT MEASURED. Regression/CLI synthetic durations are not production SLOs; no remote workload or soak executed. See `BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md`.

## Hosted validation handoff

Actual synthetic 300-second service soak completed, total job 6m5s; 60-second run also passed. This is not a representative production latency/capacity percentile baseline, 24-hour soak or replica HA proof. p50/p95/p99 remain NOT MEASURED. See `REMOTE_VALIDATION_EVIDENCE.md`.

## Sustained-write harness correction

Historical hour PASS was bounded/replay after 100 unique events, not sustained-write capacity. Corrected harness continuously inserts unique events with 0.1s pacing. Accelerated 125-event SQL regression is not wall-clock latency evidence; representative percentiles remain NOT MEASURED. See `REMOTE_VALIDATION_EVIDENCE.md`.
