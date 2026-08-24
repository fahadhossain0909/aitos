<p align="center">
  <img src="assets/banner.svg" alt="AITOS — AI Trading Operating System" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/docker-compose%20ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker Compose ready">
  <img src="https://img.shields.io/badge/exchange-Binance%20USDT--M%20Futures-F0B90B?style=flat-square" alt="Binance USDT-M Futures">
  <img src="https://img.shields.io/badge/status-active%20development-blue?style=flat-square" alt="Active development">
</p>

# AITOS — AI Trading Operating System

AITOS is an event-driven trading system for Binance USDT-M Futures. The current repository combines market-data ingestion, Redis Streams, ClickHouse persistence, optional Neo4j knowledge-graph support, risk controls, opportunity scanning, paper trading, guarded live execution, XAI/journaling, continual learning, historical replay/backtesting, health/metrics endpoints, and Docker-based deployment.

> **Canonical documentation:** this README is the single consolidated project guide. It is intentionally focused on behavior and configuration that match the current repository; obsolete setup templates, duplicate deployment instructions, historical test counts, and superseded CI/CD examples are excluded.

## 🧭 Interactive Index

Click any topic below to jump directly to that section.

### 🚀 Getting Started

- [System at a glance](#system-at-a-glance)
- [Current capabilities](#current-capabilities)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Runnable entrypoints](#runnable-entrypoints)

### 📈 Trading & Research

- [Backtesting](#backtesting)
- [Paper trading](#paper-trading)
- [Live trading](#live-trading)

### 🏗️ Deployment & Operations

- [Docker and VPS deployment](#docker-and-vps-deployment)
- [Dedicated VPS data disk](#dedicated-vps-data-disk)
- [CI/CD](#cicd)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Storage and maintenance](#storage-and-maintenance)

### 📚 Reference

- [Project structure](#project-structure)
- [Safety boundaries](#safety-boundaries)

---

## System at a glance

```text
                 Binance USDT-M Futures
                   REST / WebSocket
                          │
                          ▼
                 Market-data ingestion
                          │
                          ▼
                 Redis Streams Event Bus
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
      Risk Engine   Opportunity Scanner   Data/Journal
          │               │                │
          └───────────────┼────────────────┘
                          ▼
                   Trade Lifecycle
                     │         │
                     ▼         ▼
                   Paper      Live
                  Execution  Execution

   ClickHouse ── market history / journals / learning data
   Neo4j      ── optional knowledge graph
   XAI/ML/RL  ── explanations and continual-learning feedback
```

The core application is event-driven: components communicate through the Event Bus rather than relying on broad direct coupling. Production actions are additionally protected by risk controls and human-approval governance.

[↑ Back to Index](#-interactive-index)

## Current capabilities

| Area | Current implementation |
|---|---|
| Exchange | Binance USDT-M Futures REST + WebSocket adapter |
| Streaming | Redis Streams, consumer groups, ACK/DLQ handling, replay/request-reply |
| Persistence | ClickHouse for market data, journals and learning experience |
| Knowledge graph | Optional Neo4j integration |
| Intelligence | Opportunity scoring, market structure, volatility, CVD/order-flow, liquidity, funding, open interest and RL-related signals |
| Risk | Risk score, hard/soft limits, circuit breaker, correlated/sector exposure checks, position sizing and adaptive leverage |
| Execution | Simulated paper execution and separately guarded live execution |
| Live protection | Exchange-side protection/reconciliation where configured |
| Explainability | Trade explanations, counterfactuals and attention-based XAI |
| Learning | Online RL/ML feedback and continual-learning worker |
| Backtesting | Lightweight historical runner and richer L2/futures replay path |
| Operations | Health/metrics endpoints, structured JSON logging, Docker Compose |
| Deployment | GitHub Actions CI/CD with Docker and SSH-based VPS deployment |

[↑ Back to Index](#-interactive-index)

## Quick start

### 1. Clone and create the environment

```bash
git clone https://github.com/fahadhossain0909/aitos.git
cd aitos
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Start local infrastructure

```bash
docker compose up -d redis clickhouse neo4j
```

The default paper-trading Docker service expects all three infrastructure services to become healthy. Redis is required for the Event Bus; ClickHouse and Neo4j are optional only for code paths that explicitly support running without persistence/graph storage.

### 3. Run paper trading locally

```bash
python3 run_paper_trading.py
```

The paper runner consumes public Binance market data and uses simulated execution. Binance API credentials are not required.

Health check:

```bash
curl http://localhost:8090/health
```

### 4. Run tests

```bash
PYTHONPATH=. pytest -v
```

Backtest-only tests:

```bash
python -m pytest -q tests/backtest
```

No fixed test count is documented because the suite changes as the project evolves.

[↑ Back to Index](#-interactive-index)

## Configuration

The tracked template is `.env.example`. Copy it to `.env` and keep the real file out of Git.

Important production values include:

```text
REDIS_PASSWORD
CLICKHOUSE_PASSWORD
NEO4J_PASSWORD
```

For live execution, also configure:

```text
BINANCE_API_KEY
BINANCE_API_SECRET
BINANCE_TESTNET=true
BINANCE_HEDGE_MODE=false
```

Keep `BINANCE_TESTNET=true` until live execution is deliberately enabled. Never grant withdrawal permission to a trading API key.

### Configuration rules

- Use strong, unique passwords for Redis, ClickHouse and Neo4j.
- Keep `REQUIRE_HUMAN_APPROVAL_FOR_PROD=true` for production operation.
- Do not commit `.env` or real credentials.
- `COMPOSE_PROJECT_NAME` is `aitos`.
- `BACKTEST_DATA_DIR` is used for optional local/historical replay data and cache.

[↑ Back to Index](#-interactive-index)

## Runnable entrypoints

| Command | Purpose | Endpoint |
|---|---|---|
| `python3 run_paper_trading.py` | Live public market data + simulated orders | `8090` |
| `python3 run_live_trading.py` | Guarded real/testnet execution | `8091` |
| `python3 run_continual_learning.py` | Continual-learning worker | — |
| `python3 -m aitos.backtest.cli` | Historical backtesting | — |
| `python3 -m aitos.backtest.rich_cli` | L2/futures historical replay | — |

[↑ Back to Index](#-interactive-index)

## Backtesting

Two supported interfaces are available:

```bash
python3 -m aitos.backtest.cli --help
python3 -m aitos.backtest.rich_cli --help
```

The richer replay path models historical trade/order-book events and supports L2 execution, queue lifecycle simulation, futures margin, funding and configurable decision strategies. The canonical historical runner implementation is `aitos/backtest/aitos_runner.py`.

For a lighter dependency set:

```bash
pip install -r requirements-backtest.txt
```

The Docker Compose backtest service is profile-gated:

```bash
docker compose --profile backtest run --rm aitos-backtest --help
```

[↑ Back to Index](#-interactive-index)

## Paper trading

```bash
python3 run_paper_trading.py
```

Paper trading uses live Binance public market data but does **not** submit real exchange orders. It is the recommended first runtime mode.

When using the Docker service:

```bash
docker compose up -d
docker compose ps --all
docker compose logs -f aitos-paper
```

The default Compose paper service waits for Redis, ClickHouse and Neo4j health checks before starting.

[↑ Back to Index](#-interactive-index)

## Live trading

**Live trading is safety-critical. `run_live_trading.py` can place real orders.**

Before enabling it:

1. Test with Binance testnet first.
2. Verify API permissions and never enable withdrawals.
3. Confirm `BINANCE_HEDGE_MODE` matches the account's position mode.
4. Verify risk limits, circuit-breaker behavior and human approval.
5. Start only when you explicitly intend to submit orders.

The Docker live service is behind the `live` Compose profile and is not started by the normal default stack:

```bash
docker compose --profile live run --rm aitos-live
```

Do not use the live profile as a substitute for understanding the live-trading entrypoint and its approval flow.

[↑ Back to Index](#-interactive-index)

## Docker and VPS deployment

### Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker compose version
```

### Clone and configure

```bash
git clone https://github.com/fahadhossain0909/aitos.git
cd aitos
cp .env.example .env
chmod 600 .env
```

For production, replace every secret/example value before startup.

### Start the stack

```bash
docker compose up -d --build
docker compose ps --all
docker compose logs --tail=200 aitos-paper
curl http://localhost:8090/health
```

The Compose stack contains Redis, ClickHouse, Neo4j, paper trading, continual learning, storage maintenance, a profile-gated backtester and a profile-gated live trader.

### Update an existing VPS deployment

```bash
git pull --ff-only
docker compose up -d --build
```

### Stop

```bash
docker compose down
```

Do **not** use `docker compose down -v` during normal maintenance. It removes persistent volumes and can destroy stored data.

### Network exposure

The Compose configuration binds infrastructure and health/metrics ports to localhost. Keep Redis, ClickHouse and Neo4j inaccessible from the public internet. If remote health access is required, use an SSH tunnel:

```bash
ssh -L 8090:localhost:8090 user@your-vps
```

### Resource-aware services

The continual-learning worker is explicitly capped at 0.5 CPU / 512 MB RAM. The backtest service is capped at 2 CPU / 3 GB RAM. Storage maintenance has explicit ClickHouse/cache budgets. These limits are intended to reduce resource contention on a VPS.

[↑ Back to Index](#-interactive-index)

## Dedicated VPS data disk

Production VPS deployments should use a dedicated data disk for database and persistent application storage. The boot disk should remain separate from database storage.

A typical layout is:

```text
/dev/sdb1  50 GB   → /
/dev/sda   150 GB  → /mnt/aitos-data
```

The AITOS data disk is organized as:

```text
/mnt/aitos-data
├── clickhouse
├── neo4j
└── redis
```

### Important safety warning

The formatting step below is **destructive**. Only run `mkfs.ext4` after confirming that the selected disk is the intended empty data disk. Never blindly assume that `/dev/sda` is the data disk on another VPS; device names can differ between machines.

### Step 1 — Identify the disks

```bash
lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS,UUID
```

Confirm which disk is the boot disk and which disk is the dedicated data disk.

### Step 2 — Check that the intended data disk is empty

Replace `/dev/sda` below if your data disk has a different device name:

```bash
wipefs -n /dev/sda
```

If this prints a filesystem signature, **stop** and inspect the disk. Do not format it.

### Step 3 — Create an ext4 filesystem

Only after confirming the disk is empty:

```bash
mkfs.ext4 -L aitos-data /dev/sda
```

This creates the filesystem and automatically generates a UUID.

### Step 4 — Get the UUID

```bash
blkid /dev/sda
```

Example:

```text
/dev/sda: LABEL="aitos-data" UUID="<generated-uuid>" TYPE="ext4"
```

### Step 5 — Create the mount point

```bash
mkdir -p /mnt/aitos-data
```

### Step 6 — Configure persistent mounting

Add the UUID to `/etc/fstab`:

```text
UUID=<generated-uuid> /mnt/aitos-data ext4 defaults,nofail 0 2
```

Then reload systemd:

```bash
systemctl daemon-reload
```

Mount the disk:

```bash
mount /mnt/aitos-data
```

### Step 7 — Create AITOS storage directories

```bash
mkdir -p \
  /mnt/aitos-data/clickhouse \
  /mnt/aitos-data/neo4j \
  /mnt/aitos-data/redis
```

### Step 8 — Verify the mount

```bash
mountpoint /mnt/aitos-data
df -hT /mnt/aitos-data
ls -ld /mnt/aitos-data/{clickhouse,neo4j,redis}
```

Expected structure:

```text
/mnt/aitos-data
├── clickhouse
├── neo4j
└── redis
```

### Step 9 — Test persistence

A remount test verifies that `/etc/fstab` is correct without requiring a reboot:

```bash
umount /mnt/aitos-data
mount /mnt/aitos-data
mountpoint /mnt/aitos-data
df -hT /mnt/aitos-data
```

The mounted filesystem should resolve to the dedicated data disk, not the boot disk.

### Complete first-time setup — combined command block

The following block combines the first-time setup steps. **Review the `DATA_DISK` value before running it.** It intentionally aborts if the selected disk already has a filesystem signature.

```bash
set -e

DATA_DISK=/dev/sda
MOUNT_POINT=/mnt/aitos-data

# Verify the selected disk.
lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS,UUID "$DATA_DISK"

# Safety check: never format a disk that already has a filesystem signature.
if [ -n "$(wipefs -n "$DATA_DISK")" ]; then
  echo "ERROR: $DATA_DISK contains a filesystem signature. Aborting."
  exit 1
fi

# Create filesystem and obtain its UUID.
mkfs.ext4 -L aitos-data "$DATA_DISK"
UUID=$(blkid -s UUID -o value "$DATA_DISK")
[ -n "$UUID" ]

echo "Created filesystem UUID: $UUID"

# Configure persistent mount.
mkdir -p "$MOUNT_POINT"
sed -i '\|[[:space:]]/mnt/aitos-data[[:space:]]|d' /etc/fstab
echo "UUID=$UUID $MOUNT_POINT ext4 defaults,nofail 0 2" >> /etc/fstab

systemctl daemon-reload
mount "$MOUNT_POINT"

# Create persistent AITOS storage directories.
mkdir -p \
  "$MOUNT_POINT/clickhouse" \
  "$MOUNT_POINT/neo4j" \
  "$MOUNT_POINT/redis"

# Final verification.
mountpoint "$MOUNT_POINT"
df -hT "$MOUNT_POINT"
ls -ld \
  "$MOUNT_POINT/clickhouse" \
  "$MOUNT_POINT/neo4j" \
  "$MOUNT_POINT/redis"
```

> **Do not run the combined first-time setup block on an existing production data disk.** Once the filesystem has been created, use the verification/remount commands above instead. Running `mkfs.ext4` again will destroy the filesystem contents.

[↑ Back to Index](#-interactive-index)

## CI/CD

The authoritative workflows are:

- `.github/workflows/ci.yml`
- `.github/workflows/cd.yml`

### CI pipeline

CI runs on pushes and pull requests targeting `main`, `master` and `develop`.

It currently includes:

- Black, isort and Flake8 checks.
- Tests on Python 3.10, 3.11 and 3.12.
- ClickHouse service integration for tests requiring it.
- Coverage and JUnit test-result artifacts.
- Bandit and dependency security checks.
- Docker Compose validation and Docker image build validation.
- A final results job that fails when a required job actually fails.

**Important audit note:** the current lint and security commands use `continue-on-error: true`. Therefore lint/security findings can be reported without blocking the workflow; the test and Docker jobs remain blocking through the final results job.

The test workflow also contains explicit checkout/import diagnostics around `aitos/kernel/decision_fusion.py`. These are diagnostic safeguards for detecting checkout/import regressions, not application functionality.

### CD pipeline

CD runs on pushes to `main`/`master`, version tags and manual dispatch.

Current deployment flow:

1. Checkout repository.
2. Build and publish the Docker image to GHCR.
3. Use the `papertrade` GitHub Environment for deployment.
4. Verify required deployment/database secrets.
5. SSH to the VPS.
6. Maintain the application at `~/aitos`.
7. Generate a protected `.env` from GitHub Secrets.
8. Validate `docker compose` configuration.
9. Pull/build and start the stack.
10. Wait for ClickHouse to become healthy and print diagnostics if it does not.

The deployment uses `COMPOSE_PROJECT_NAME=aitos` and `${{ github.repository }}` rather than a hard-coded repository name.

### Required CD secrets

The workflow explicitly requires:

```text
DEPLOY_HOST
DEPLOY_USER
DEPLOY_SSH_KEY
REDIS_PASSWORD
CLICKHOUSE_PASSWORD
NEO4J_PASSWORD
```

Binance credentials are conditional: they are not required for paper/testnet operation, but both API key and API secret are required when `BINANCE_TESTNET=false`.

Other optional deployment values include `CLICKHOUSE_USER`, `NEO4J_USER`, `DEPLOY_PORT`, `SENTRY_DSN` and `SLACK_WEBHOOK_URL`.

### Deployment caution

The current CD workflow publishes an image to GHCR but also runs `docker compose up -d --build` on the VPS. Therefore the deployment currently performs a local build on the server rather than relying exclusively on the published GHCR image. This is a known operational characteristic of the current workflow and should not be confused with a pure image-pull deployment.

[↑ Back to Index](#-interactive-index)

## Operations and troubleshooting

### Container status

```bash
docker compose ps --all
docker stats --no-stream
```

### Logs

```bash
docker compose logs --tail=200 aitos-paper
docker compose logs --tail=200 aitos-learning
docker compose logs --tail=200 aitos-clickhouse
docker compose logs --tail=200 redis
```

### Health

```bash
curl http://localhost:8090/health
```

For live health/metrics, use the corresponding `8091` endpoint when the live service is intentionally running.

### ClickHouse unhealthy

If ClickHouse becomes unhealthy:

```bash
docker compose ps --all
docker compose logs --tail=300 aitos-clickhouse
docker inspect --format='{{json .State.Health}}' aitos-clickhouse
```

Identify the health-check or configuration failure before repeatedly rebuilding or restarting the stack.

### RAM and disk pressure

```bash
free -h
df -h
docker stats --no-stream
docker system df
```

Pay particular attention to ClickHouse storage, container logs, historical/backtest data and model volumes. Preserve database volumes during normal cleanup.

[↑ Back to Index](#-interactive-index)

## Storage and maintenance

### Storage roles

- **Redis:** real-time Event Bus transport/state; required by the application runtime.
- **ClickHouse:** long-lived market history, journal data and learning experience.
- **Neo4j:** optional knowledge graph.
- **Docker named volumes:** persistent service/model storage.
- **Backtest data directory:** local/historical replay data and cache.

### Maintenance service

`aitos-storage-maintenance` runs `aitos.storage.maintenance` and is configured by Compose with explicit storage/cache budgets. It is intended to prevent historical/backtest storage from growing without bounds.

Before manually deleting data, inspect the maintenance configuration and current disk usage. Never use `docker compose down -v` as a routine cleanup command.

[↑ Back to Index](#-interactive-index)

## Project structure

```text
aitos/
├── aitos/
│   ├── agents/             # agent framework
│   ├── backtest/           # historical replay/backtesting
│   ├── core/               # contracts and core abstractions
│   ├── data/               # ingestion and historical data
│   ├── eventbus/           # Redis Streams Event Bus
│   ├── exchange/           # Binance adapter and exchange utilities
│   ├── execution/          # paper/live execution
│   ├── intelligence/       # scanners, indicators, RL/ML signals
│   ├── journal/             # trade journaling and reviews
│   ├── kernel/              # AI Kernel and decision fusion
│   ├── knowledge_graph/     # optional Neo4j integration
│   ├── learning/            # continual-learning support
│   ├── risk/                # risk engine and position sizing
│   ├── storage/             # storage maintenance
│   ├── trading/             # trade lifecycle/reconciliation
│   └── xai/                 # explanations and model interpretability
├── tests/                   # unit/integration/backtest tests
├── docs/                    # focused technical documentation
├── deploy/                  # service/deployment assets
├── .github/workflows/       # CI/CD
├── docker-compose.yml       # local/VPS service topology
├── Dockerfile               # application image
├── .env.example              # tracked configuration template
├── requirements.txt          # main runtime dependencies
└── requirements-backtest.txt # lighter backtest dependencies
```

[↑ Back to Index](#-interactive-index)

## Safety boundaries

AITOS is an actively developed trading system, not a guarantee of trading performance.

- Paper trading should be used before real execution.
- Live execution requires deliberate human approval.
- Risk controls are part of the execution path, but operators remain responsible for configuration and exchange permissions.
- Testnet and production credentials must be kept separate.
- Do not expose Redis, ClickHouse or Neo4j directly to the internet.
- Do not grant withdrawal permission to trading API keys.
- Treat automated learning as bounded by the project's configured governance and deployment controls.

[↑ Back to Index](#-interactive-index)
