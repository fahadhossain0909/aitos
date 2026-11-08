# Production statistical drift and approval adapters

## Runtime wiring

Construct `ClickHouseDriftFeatureBatchProvider` with the existing
`ClickHouseExperienceStore` (or a compatible read-only store), an explicit
feature allow-list, disjoint historical windows, and a minimum sample count.
It reads only closed experiences from configured sources. Missing, stale,
non-numeric, non-finite, or undersized samples raise instead of reporting
NORMAL. Keep the provider's clock UTC and synchronize the ClickHouse host.

Use `LoggingDriftAlertSink` as the no-network default, or configure
`WebhookDriftAlertSink` with an HTTPS endpoint and bearer token injected from
Render Environment Groups / secret storage. The scheduler already deduplicates
unchanged incidents. Webhook failures propagate so the managed worker reports
delivery failure; configure retries/dead-letter handling in the host worker.

## Authenticated approval boundary

`OperatorApprovalService` is a service-layer boundary, not an identity
provider or HTTP server. The API must validate OIDC/SAML/mTLS credentials and
construct `OperatorIdentity` from verified claims; never accept issuer,
subject, roles, or `authenticated` from request JSON. Restrict the approval
endpoint to a dedicated approver role and require TLS. Keep
`AITOS_APPROVAL_SIGNING_SECRET` (at least 32 random bytes) in Render secrets,
not in source control. The token is short-lived, HMAC-signed, bound to the
candidate artifact SHA-256, operator, and required 8–500 character approval
rationale, and consumed once using a SQLite
transaction. The SQLite WAL implementation is suitable only when all approval
requests reach one service instance using the same durable local persistent
disk. Do not place this file on a network filesystem or share it between
Render instances. For a multi-instance API, replace the nonce repository with
a transactional PostgreSQL implementation before scaling out. Render
ephemeral filesystems are not durable; do not enable production approvals
there.

The approval service consumes the nonce before attempting promotion. A failed
promotion therefore requires a fresh approval, preventing replay. Append-only
audit events record issuance, approver identity and roles, candidate digest,
approval rationale, nonce consumption, and the final promotion decision.
SQLite triggers reject audit-row updates and deletes. Rollback remains a
separate explicitly authorized operation and must be exposed through an
independently authenticated endpoint.

## Safety and deployment

- Do not start drift monitoring until ClickHouse, feature schema, feature
  allow-list, sample thresholds and alert sink have been verified.
- A provider failure is a monitoring failure, never a NORMAL drift result.
- Drift detection blocks candidate promotion; it does not stop trading,
  liquidate positions, or promote/rollback models.
- Do not enable a public approval endpoint until real identity middleware,
  RBAC, rate limiting, CSRF protections where applicable, and audit retention
  are configured.
- No secrets, production URLs, or credentials are committed by this change.
- Paper/shadow validation and CI acceptance remain required. No live trading
  or automatic promotion is enabled.

## Feature configuration example

Choose an explicit allow-list that matches the numeric keys actually written into `learning_experiences.features_json`, for example `("return_1m", "realized_volatility", "spread_bps", "cvd_zscore", "ofi_zscore", "slippage_bps", "fill_ratio")`. Do not enable features until their producer, units, sampling cadence, and missing-value policy have been audited. Tune `window`, `lookback`, `max_staleness`, and `min_observations` from the observed data cadence; defaults are conservative examples, not universal production thresholds.


## Paper-runtime drift monitor activation

The paper runtime can start the managed A-Stat drift scheduler, but it is
**disabled by default**. Enable it only after the deployment owner has chosen
and validated the feature schema represented by closed learning experiences.

Required configuration:

- `AITOS_STATISTICAL_DRIFT_ENABLED=true`
- `AITOS_STATISTICAL_DRIFT_FEATURES=feature_a,feature_b` — exact numeric
  keys present in `learning_experiences.features_json`; there is no inferred
  or wildcard feature selection.
- `AITOS_STATISTICAL_DRIFT_SOURCES=paper` — comma-separated
  `paper,live,backtest` sources; production paper deployments should normally
  start with `paper` only.
- Optional `AITOS_STATISTICAL_DRIFT_MIN_OBSERVATIONS=200` and
  `AITOS_STATISTICAL_DRIFT_INTERVAL_SECONDS=300`.

Alerts use structured application logs by default. To use an operator-owned
HTTPS endpoint, configure both
`AITOS_STATISTICAL_DRIFT_WEBHOOK_URL` and
`AITOS_STATISTICAL_DRIFT_WEBHOOK_TOKEN` as deployment secrets. Redirects are
rejected and delivery failures are logged; the scheduler retries on the next
cadence.

The provider queries disjoint reference/current windows and fails closed when
the feature schema is missing, samples are undersized, values are malformed,
or the newest current observation is stale. A provider failure does not stop
the paper scanner or silently report NORMAL; the managed scheduler logs the
failure and retries. Drift blocks statistical model promotion only. It does
not close positions, change entry gates, or auto-promote/rollback models.

The scheduler and its dedicated ClickHouse client are stopped during paper
runtime shutdown. The SQLite model registry is local to the runtime instance;
it is not a multi-instance coordination service. Use a durable shared registry
before scaling this lifecycle horizontally.


## Paper-runtime Hawkes bootstrap

Marked-Hawkes live observation is available behind an explicit deployment
allow-list and is **disabled by default**. To bootstrap candidate filters:

- `AITOS_HAWKES_ENABLED=true`
- `AITOS_HAWKES_SYMBOLS=BTCUSDT,ETHUSDT` — explicit symbols only.
- `AITOS_HAWKES_SAMPLE_LIMIT=1000` — recent public REST trades per symbol
  (100–1000).
- `AITOS_HAWKES_MIN_EVENTS=200` and
  `AITOS_HAWKES_MIN_MARK_EVENTS=20` — minimum sample and each aggressor mark.
- `AITOS_HAWKES_HOLDOUT_FRACTION=0.2` — chronological holdout, never shuffled.
- `AITOS_HAWKES_DECAY_GRID=0.05,0.1,0.2,0.5,1.0` and
  `AITOS_HAWKES_MAX_ITERATIONS=120` — bounded fit budget.
- `AITOS_HAWKES_MIN_OOS_GAIN_PER_EVENT=0` — require positive held-out
  log-likelihood gain per event against the training-rate Poisson baseline.

Each symbol is fetched independently. Duplicate millisecond timestamps are
made strictly chronological using the next representable float; genuinely
out-of-order live events are rejected. A model is not installed unless the MLE
converges, its branching ratio is stationary, both marks have adequate samples,
and the chronological holdout beats the Poisson baseline. Accepted history is
replayed into the online filter before the filter is atomically registered.

Hawkes intensities are currently **telemetry/state only**. They do not alter
scanner scores, entry gates, position sizing, or exits. Decision-path use
requires a separate calibrated feature contract, walk-forward/OOS acceptance,
ablation against the no-Hawkes baseline, and operator approval. A rejected or
undersized model leaves the symbol on the existing A-Stat path.


The current bootstrap keeps the accepted fit in process memory and re-fits from
the bounded public-trade sample on each process start. It does not persist or
promote a champion artifact, and it is not a multi-instance model registry.
Persisted signed model artifacts, rolling refit cadence, champion/challenger
comparison and rollback remain separate governance work.
