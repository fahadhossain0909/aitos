# AITOS Production Reliability V1

PR #116 now establishes the reliability boundary for the canonical market-data and trading runtime.

## Guarantees

1. **Bounded work** — retry attempts, backoff, concurrency and market-data queues are bounded. No unbounded backlog is allowed on the live path.
2. **Fail closed** — stale evidence, risk rejection and unavailable required dependencies cannot silently produce an executable trade.
3. **Replayability** — critical runtime events can be written to an append-only, hash-chained journal and replayed deterministically.
4. **Recovery** — exchange-side reconciliation runs after startup and periodically so missed fills do not leave stale positions indefinitely.
5. **Integration verification** — CI exercises real Redis, ClickHouse and Neo4j containers in addition to unit tests.
6. **Observability** — `/health` and `/metrics` expose module state/counters; an optional Prometheus + Grafana profile is supplied.
7. **Immutable deployment** — CD deploys a commit-addressed GHCR image rather than rebuilding on the VPS. `scripts/rollback_deploy.sh` restores a previously published image tag.
8. **Secrets boundary** — production secret access can use environment variables during the current VPS phase or Vault KV v2 through one provider interface. Secrets are not committed to the repository.
9. **Controlled chaos** — `scripts/chaos_smoke.sh` restarts Redis, Neo4j and the paper application and verifies recovery. It refuses to run with `ENVIRONMENT=production`.

## Runtime composition

```text
Market Data -> bounded gateway -> Redis Streams -> live consumers
                         |                 |
                         |                 +-> ClickHouse persistence (bounded/lossy history queue)
                         +-> health/metrics

Decision -> policy/risk gate -> ExecutionIntent -> exchange adapter
                 |
                 +-> append-only decision/event journal -> replay/recovery

Startup/reconnect -> exchange reconciliation -> internal position state
```

## Deliberate non-goals

Kubernetes/Helm/ArgoCD and multi-node canary orchestration are not required for the current single-VPS deployment. Introducing them before the trading runtime needs horizontal orchestration would add operational failure modes without improving trading correctness. The deployment boundary remains Docker Compose + immutable GHCR images + health gates + explicit rollback.

Full OpenTelemetry/Jaeger is likewise an integration extension rather than a prerequisite for the existing health/metrics contract. The system is structured so tracing can be added around the same module/event boundaries without changing strategy logic.
