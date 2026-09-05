# AITOS Staging Deployment

AITOS now supports a production-like pre-merge staging path for branch validation.

## Deployment model

```text
feature/* / bugfix/* / hotfix/* / architecture/*
                    |
                    v
              GitHub CI gates
                    |
                    v
          Staging CD + Runtime Audit
                    |
                    v
              STAGING VPS
                    |
                 PASS
                    |
                    v
                  main
                    |
                    v
              Production BPS
```

The production CD workflow remains restricted to `main`, `master`, and version tags. Staging is deliberately separate and uses an isolated data root and separate GitHub environment/secrets.

## Required GitHub environment

Create a GitHub Actions environment named `staging` and add these secrets:

Required:

- `STAGING_DEPLOY_HOST`
- `STAGING_DEPLOY_USER`
- `STAGING_DEPLOY_SSH_KEY`
- `STAGING_DATA_DISK_UUID`
- `STAGING_REDIS_PASSWORD`
- `STAGING_CLICKHOUSE_PASSWORD`
- `STAGING_NEO4J_PASSWORD`

Optional:

- `STAGING_BINANCE_API_KEY`
- `STAGING_BINANCE_API_SECRET`
- `STAGING_BINANCE_TESTNET` (defaults to `true`)
- `STAGING_DATABASE_URL`
- `STAGING_SENTRY_DSN`

Never reuse production secrets unless the staging account is intentionally isolated and approved for that purpose. Prefer Binance testnet credentials for staging.

## Staging VPS requirements

Use a dedicated VM/VPS. Do not run staging on the production BPS because the compose file intentionally uses fixed AITOS container names and the staging deployment is allowed to recreate its own stack.

Recommended baseline:

- 2 vCPU
- 4–8 GB RAM
- 50–100 GB persistent disk
- Docker Engine + Docker Compose v2
- GitHub Actions SSH access
- a dedicated data disk mounted at `/mnt/aitos-staging-data`

The data disk UUID must match `STAGING_DATA_DISK_UUID`. The deployment calls `scripts/bootstrap_storage.sh` and fails closed if the configured data root is not a mount point.

## What staging validates

Every supported branch push builds an immutable GHCR image tagged with its commit SHA, deploys that exact image to staging, waits for Redis/ClickHouse/Neo4j health, validates canonical storage paths, and runs the read-only `scripts/paper_runtime_audit.sh` audit.

The audit also captures:

- live scanner freshness diagnostics
- paper signal diagnostics
- scanner score breakdowns
- scanner ranking/trade-decision diagnostics
- container state
- tested commit and branch

A non-zero audit exit code fails the workflow. A passing staging run is the pre-merge runtime gate; production deployment remains a separate step after merge to `main`.

## Data and isolation rules

Production uses `/mnt/aitos-data`. Staging uses `/mnt/aitos-staging-data`.

Production compose project: `aitos`

Staging compose project: `aitos-staging`

Staging deployment directory: `$HOME/aitos-staging`

Staging audit directory: `$HOME/aitos-staging-audit`

The staging workflow never runs global Docker prune/cleanup and never targets the production data root.

## First-time setup

1. Provision the dedicated staging VM and attach a persistent data disk.
2. Format/mount the disk and record its filesystem UUID.
3. Install Docker Engine and Docker Compose v2.
4. Create a dedicated SSH key for GitHub Actions.
5. Add the required `staging` environment secrets.
6. Push any supported branch.
7. Confirm `Staging CD + Runtime Audit` builds, deploys, and passes the runtime audit.
8. Only then merge the tested branch into `main`.

## Operational rule

Staging is for proving code against a production-like runtime. It is not a production trading environment. Keep trading disabled or on testnet/paper mode and keep production credentials/data isolated.
