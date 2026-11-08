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
candidate artifact SHA-256 and operator, and consumed once using a SQLite
transaction. Use a persistent, backed-up database path shared by all approval
API replicas; local ephemeral storage is not suitable for production.

The approval service consumes the nonce before attempting promotion. A failed
promotion therefore requires a fresh approval, preventing replay. Audit rows
record nonce, approver, candidate digest, expiry, consumption and final
decision. Rollback remains a separate explicitly authorized operation and must
be exposed through an independently authenticated endpoint.

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
