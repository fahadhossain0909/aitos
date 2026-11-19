# AEP-1 Managed Runtime Governance Lifecycle

## Scope

The application composition root now accepts two explicit optional dependencies:

- `governance_drift_scheduler`: a `DriftMonitorScheduler` configured with a `StatisticalGovernanceRuntime`
- `governance_drift_batch_provider`: a synchronous or asynchronous zero-argument provider returning the timestamp-aligned `(reference_features, current_features)` batches required by the scheduler

They must be configured together. Supplying only one is rejected by `build_system()`.

## Lifecycle ownership

`initialize_all()` starts one named asyncio task after core modules and event subscriptions are initialized. Repeated initialization does not create a duplicate task while the existing task is active.

`shutdown_all()` signals the cooperative stop event and waits up to the configured shutdown grace period. A task that exceeds the grace period is cancelled and reported. The scheduler is not created at import time and remains disabled when no dependencies are supplied.

## Governance behavior

The scheduler consumes the caller's feature batches and records NORMAL, WARNING, CRITICAL or UNAVAILABLE drift state. It deduplicates alert callbacks by state fingerprint. A failed provider call marks drift UNAVAILABLE; that state blocks model promotion. Alert delivery failures are logged and do not terminate the monitoring loop.

The scheduler does not:
- retrain or refit models;
- mutate scanner features, scores or thresholds;
- change capital allocation or risk limits;
- close positions or cancel orders;
- promote or roll back a champion;
- authenticate an operator.

Promotion and rollback remain separate explicit governance operations requiring the existing statistical evidence and operator approval contracts.

## Deployment requirements still external

The caller must supply point-in-time-safe, schema-compatible feature batches and a suitable alert callback. A production approval interface must authenticate the human operator and issue approval only after an explicit action. This lifecycle integration is not evidence that a provider, alert route or authenticated approval UI has been configured in a target deployment.

## Verification

`tests/test_app_subscription_ownership.py` verifies scheduler startup, provider handoff, cooperative shutdown and cleanup ownership. Existing governance tests cover drift blocking, unavailable-data behavior, challenger evidence and operator approval.
