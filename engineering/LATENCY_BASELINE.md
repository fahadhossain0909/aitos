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
