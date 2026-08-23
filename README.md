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

AITOS is an event-driven trading system for Binance USDT-M Futures. The repository currently combines market-data ingestion, Redis Streams, ClickHouse persistence, optional Neo4j knowledge-graph support, risk controls, opportunity scanning, paper trading, guarded live execution, XAI/journaling, continual learning, backtesting, health/metrics endpoints, and Docker-based deployment.

> **Canonical documentation:** this README is the consolidated project guide. Older standalone setup/deployment/CI documents contained duplicated material, generic templates, stale test counts, or configuration that no longer matches the current repository and are intentionally not treated as authoritative.

## Quick start

### Local paper trading

```bash
git clone https://github.com/fahadhossain0909/aitos.git
cd aitos
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d redis clickhouse neo4j
python3 run_paper_trading.py
```

Health endpoint:

```text
http://localhost:8090/health
```

Redis is the real-time Event Bus and is required by the application. ClickHouse and Neo4j are optional for components that can operate without persistence/graph storage.

### Tests

```bash
PYTHONPATH=. pytest -v
```

Backtest-only tests:

```bash
python -m pytest -q tests/backtest
```

The repository has grown beyond the old `291 tests` documentation claim; no fixed test count is maintained here.

## Architecture

```text
Binance REST / WebSocket
          │
          ▼
   Redis Streams Event Bus
          │
    ┌─────┼───────────────────────────┐
    ▼     ▼                           ▼
  Risk  Opportunity Scanner       Data/Journaling
  Engine       │                     │
    │          ▼                     ▼
    └────► Trade Lifecycle       ClickHouse
               │
          ┌────┴────┐
          ▼         ▼
       Paper      Live
      Execution  Execution

Neo4j Knowledge Graph = optional
Continual Learning    = background worker
XAI / Journal         = explanations + trade records
```

### Main capabilities

- Binance USDT-M Futures REST/WebSocket market-data adapter with rate limiting and reconnect handling.
- Redis Streams Event Bus with consumer groups, acknowledgements, replay/request-reply and DLQ handling.
- Risk scoring, circuit breaker, hard/soft limits, sector exposure protection, position sizing and adaptive leverage.
- Opportunity scoring using market structure, volatility, CVD/order flow, auction context, liquidity, funding, open interest, lead-lag and RL signals.
- Paper trading and separately guarded live execution.
- Exchange-side protection/reconciliation for live orders where configured.
- ClickHouse market-data, journal and learning-experience persistence.
- Optional Neo4j knowledge graph.
- XAI explanations, counterfactuals and attention-based explanations.
- Continual-learning worker and online RL/ML feedback components.
- Lightweight and full L2/futures historical replay backtesting.
- Health/metrics endpoints, structured JSON logging and Docker Compose deployment.

## Runnable entrypoints

| Entrypoint | Purpose | Health |
|---|---|---:|
| `run_paper_trading.py` | Live Binance public data with simulated orders | `8090` |
| `run_live_trading.py` | Guarded real/testnet execution | `8091` |
| `run_continual_learning.py` | Background continual-learning worker | — |
| `python3 -m aitos.backtest.cli` | Historical backtesting | — |
| `python3 -m aitos.backtest.rich_cli` | Rich L2/futures historical replay | — |

## Local development

### Requirements

- Python 3.10–3.12; Python 3.12 is the primary project/Docker version.
- Docker and Docker Compose.
- Git.

### Backtest dependencies

For a lighter backtesting environment:

```bash
pip install -r requirements-backtest.txt
```

### Infrastructure

```bash
docker compose up -d redis clickhouse neo4j
```

### Configuration

```bash
cp .env.example .env
```

Paper trading does not require Binance API credentials because it uses public market data and simulated execution.

## Backtesting

Two backtesting paths are available:

```bash
python3 -m aitos.backtest.cli --help
python3 -m aitos.backtest.rich_cli --help
```

The rich runner supports historical trade/order-book events, L2 execution, queue lifecycle simulation, futures margin, funding and configurable decision strategies. The canonical runner implementation is `aitos/backtest/aitos_runner.py`.

## Paper trading

```bash
python3 run_paper_trading.py
```

Paper trading consumes live Binance market data but does not submit real exchange orders. It can run with local Redis and optionally ClickHouse/Neo4j. Use the health endpoint and structured logs to verify operation.

## Live trading — safety critical

`run_live_trading.py` can place real orders. Before enabling it:

1. Configure Binance API credentials only when required.
2. Use Binance testnet first.
3. Verify `BINANCE_HEDGE_MODE` matches the account position mode.
4. Keep human approval enabled for production actions.
5. Never grant withdrawal permission to the API key.

The Docker Compose live profile is intentionally separate from the default paper stack.

## Production/VPS deployment

### Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker compose version
```

### Clone

```bash
git clone https://github.com/fahadhossain0909/aitos.git
cd aitos
```

### Configure secrets

```bash
cp .env.example .env
chmod 600 .env
```

Production must use non-default secrets, especially:

- `REDIS_PASSWORD`
- `CLICKHOUSE_PASSWORD`
- `NEO4J_PASSWORD`
- `BINANCE_API_KEY` / `BINANCE_API_SECRET` when live execution is enabled

Do not commit `.env`.

### Start

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f aitos-paper
curl http://localhost:8090/health
```

### Live profile

Live execution is intentionally not started by the normal `docker compose up -d` path. When explicitly required, use the guarded live profile/entrypoint and follow the human-approval requirements above.

### Update

```bash
git pull --ff-only
docker compose up -d --build
```

### Stop

```bash
docker compose down
```

`docker compose down -v` also removes persistent volumes and therefore can destroy stored data; use it only deliberately.

### Network security

The current Compose configuration binds service ports to localhost where appropriate. Keep Redis, ClickHouse and Neo4j off the public internet. For remote health/metrics access, prefer an SSH tunnel, for example:

```bash
ssh -L 8090:localhost:8090 user@your-vps
```

### ARM VPS

The Docker build includes native build tooling for dependencies that may not provide an ARM64 wheel. The first build on ARM can therefore take longer than on x86.

## CI/CD

The authoritative workflows are:

- `.github/workflows/ci.yml`
- `.github/workflows/cd.yml`

### CI

CI runs on pushes and pull requests targeting `main`, `master` and `develop`. It currently includes:

- Code-quality checks with Black, isort and Flake8.
- Unit/integration tests on Python 3.10, 3.11 and 3.12.
- A ClickHouse service for tests that need database integration.
- Coverage/test-result artifacts.
- Bandit and dependency security checks.
- Docker Compose validation and Docker image build validation.
- A final CI-results job that reports the combined status.

The test job also performs explicit source/import diagnostics before pytest; these checks are useful when investigating checkout or import-path regressions.

### CD

CD runs for pushes to `main`/`master`, version tags, and manual workflow dispatch. It:

1. Builds the Docker image.
2. Pushes the image to GHCR.
3. Uses the `papertrade` GitHub Environment for deployment.
4. Verifies required deployment/database secrets.
5. Connects to the VPS through SSH.
6. Maintains the deployment at `~/aitos`.
7. Generates a protected production `.env` from GitHub Secrets.
8. Validates Docker Compose configuration.
9. Pulls/builds and starts the stack.
10. Waits for ClickHouse health and prints diagnostics if it fails.

The deployment uses `COMPOSE_PROJECT_NAME=aitos` and the repository's current `${{ github.repository }}` path, so the old repository name is not part of the deployment contract.

### Required CD secrets

The current workflow explicitly requires:

```text
DEPLOY_HOST
DEPLOY_USER
DEPLOY_SSH_KEY
REDIS_PASSWORD
CLICKHOUSE_PASSWORD
NEO4J_PASSWORD
```

Optional/conditional values include Binance credentials and related deployment settings. Binance credentials are intentionally not required for paper/testnet operation; when `BINANCE_TESTNET=false`, the workflow requires both Binance API credentials.

> Older setup documentation listed generic `DATABASE_URL`, `API_KEY`, `SECRET_KEY` and `DEBUG` values as if they were the primary deployment configuration. Those generic/Django-style instructions are not authoritative for the current project and are not repeated here.

## Monitoring and troubleshooting

### Container status

```bash
docker compose ps --all
docker stats --no-stream
```

### Logs

```bash
docker compose logs --tail=200 aitos-paper
docker compose logs --tail=200 aitos-clickhouse
docker compose logs --tail=200 redis
```

### Health

```bash
curl http://localhost:8090/health
```

If ClickHouse is unhealthy, inspect its logs and health status before restarting the whole stack. Avoid repeatedly rebuilding containers without first identifying the failing healthcheck or configuration.

### Disk/RAM pressure

```bash
df -h
docker system df
docker stats --no-stream
```

Preserve database volumes unless data deletion is intentional. Review container logs and storage-maintenance behavior before deleting anything.

## Storage model

- **Redis:** real-time Event Bus/state required by the application.
- **ClickHouse:** canonical long-term market history, journal and learning experience.
- **Neo4j:** optional knowledge graph.

Keep persistent data on named Docker volumes and avoid `docker compose down -v` during normal maintenance.

## Repository conventions

- Canonical repository name: `aitos`.
- Use `aitos` in code, imports, filenames, Docker/Compose names, deployment paths and documentation.
- Do not introduce legacy project-name references.
- Keep secrets out of source control.
- Treat `README.md` as the canonical operational/documentation entry point.

## Current implementation boundaries

The project is actively developed. Documentation should describe only behavior that exists in the current source/configuration. Historical plans, obsolete setup templates, old test counts, generic CI/CD examples and superseded deployment instructions are intentionally excluded from this README to avoid misleading operators.

## License

See the repository license file for the applicable terms.
