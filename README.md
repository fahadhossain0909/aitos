<p align="center">
  <img src="assets/banner.svg" alt="AITOS — AI Trading Operating System" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/docker-compose%20ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker Compose ready">
  <img src="https://img.shields.io/badge/markets-Binance%20Futures%20%2B%20Spot-F0B90B?style=flat-square" alt="Binance Futures and Spot">
  <img src="https://img.shields.io/badge/status-active%20development-blue?style=flat-square" alt="Active development">
</p>

# AITOS — AI Trading Operating System

AITOS is a Python-first, event-driven quantitative trading platform. Binance USDⓈ-M Futures is the established execution core; a separately isolated Binance Spot runtime is now present but remains opt-in, testnet-first, and execution-disabled by default. The repository combines canonical market-data contracts, Redis Streams, ClickHouse history, optional Neo4j knowledge-graph support, shared portfolio/capital controls, opportunity and research intelligence, paper trading, guarded Futures execution, XAI/journaling, continual learning, historical/L2 replay, observability, and Docker-based deployment. Options are an identity/architecture extension point only; no Options strategy or execution is available.

> **Canonical documentation:** this README is the project overview. For installation, follow the [AITOS Installation & Deployment Guide](INSTALLATION.md), which documents both desktop installation and VPS runtime deployment.

## Credential security and remote signing

Read [Credential Security & Remote Signing RFC](docs/CREDENTIAL_SECURITY_REMOTE_SIGNING_RFC.md) before entering exchange credentials on a remote host. OS vaults protect local storage, not against a provider administrator with root/administrator access to that same machine. Paper-only signer and testnet adapter components exist, but production remote signing and live order dispatch are not enabled or certified.

## Desktop application delivery

AITOS currently has two distinct desktop delivery tracks; their installers and security boundaries are not interchangeable.

| Track | What it provides | Current release status |
|---|---|---|
| Native Console Session Manager | Windows/Linux launcher for the authenticated read-only browser console; connects to an already-running backend | Unsigned test installers; no backend installation or trading runner |
| Tauri Embedded Desktop | React operator workspace in a native shell, bundled read-only gateway sidecar, and a separate local Trusted Signer Settings window | Cross-platform unsigned CI candidates; not a signed/trusted production release |

The [desktop guide](desktop/README.md) describes the Tauri application. The [installation guide](INSTALLATION.md) explains the session-manager packages and the separate Tauri evaluation path. Neither desktop track installs or starts the AITOS trading backend. Tauri signer settings are not proof of a production remote signer or authorization to enable live order dispatch. Do not run signer credential custody on a provider-managed VPS/RDP whose administrator is outside your trust boundary.

## 🧭 Interactive Index

Click any topic below to jump directly to that section.

### 🚀 Getting Started

- [System at a glance](#system-at-a-glance)
- [Current capabilities](#current-capabilities)
- [Quick start](#quick-start)
- [Installation: Desktop or VPS](INSTALLATION.md)
- [VPS-free local development](docs/LOCAL_DEVELOPMENT.md)
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
- [Credential security & remote signing](docs/CREDENTIAL_SECURITY_REMOTE_SIGNING_RFC.md)
- [Desktop Operator User Manual](docs/USER_MANUAL.md)
- [Binance Spot integration and rollout limits](docs/SPOT_MARKET_INTEGRATION.md)
- [Architecture Evolution Program](engineering/ARCHITECTURE_EVOLUTION_PROGRAM.md)
- [System audit and open acceptance gates](engineering/SYSTEM_AUDIT_2026-11-20.md)
- [Financial state authority & reconciliation contract](engineering/FINANCIAL_STATE_AUTHORITY_AND_RECONCILIATION.md)

---

## System at a glance

```text
 Binance USDⓈ-M Futures       Binance Spot (opt-in)
   REST / WebSocket             REST / WebSocket
          │                           │
          ▼                           ▼
   Futures market adapter      Isolated Spot adapter
          │                           │
          └────── canonical market-data contracts ──────┐
                                                        ▼
                                              Redis Streams / MarketDataBus
                                                        │
                         ┌──────────────────────────────┼──────────────────┐
                         ▼                              ▼                  ▼
                  Opportunity /                 Portfolio &          Data / Journal /
                  Research Intelligence          CapitalGateway       Learning
                         │                              │
                         └───────────────┬──────────────┘
                                         ▼
                                  Trade / Order Lifecycle
                                     │           │
                                     ▼           ▼
                                   Paper     Guarded Futures
                                 Execution    Execution

   ClickHouse ── versioned market history, journals and learning evidence
   SQLite / scoped stores ── durable execution intents and capital reservations
   Neo4j ── optional knowledge graph; Rust/PyO3 ── optional order-book kernel
```

The core application is event-driven: components communicate through the Event Bus rather than relying on broad direct coupling. Production actions are additionally protected by risk controls and human-approval governance.

[↑ Back to Index](#-interactive-index)

## Current capabilities

| Area | Current implementation |
|---|---|
| Exchange | Binance USDⓈ-M Futures adapter; separate Binance Spot adapter (USDT quote scope, opt-in, testnet-first) |
| Market contracts | Canonical market-data envelope, market-type-scoped Spot identity, provenance-preserving domain bridge |
| Streaming | Redis Streams, consumer groups, ACK/DLQ handling, replay/request-reply, sequence-aware market-data handling |
| Persistence | ClickHouse for versioned market history, journals and learning evidence; scoped durable execution/capital stores |
| Knowledge graph | Optional Neo4j integration |
| Intelligence | Market structure, volatility, CVD/order-flow, liquidity, funding/open interest, cross-market lead-lag research, ML/RL and probabilistic decision research |
| Portfolio & risk | Shared CapitalGateway, atomic reservations, open/pending exposure projection, circuit breaker and correlated/sector checks |
| Compute | Optional adaptive CPU/memory budgets and pressure-aware admission for non-critical analysis; disabled by default and rejected in live mode |
| Execution | Simulated paper execution; separately guarded Futures live path; Spot execution disabled by default |
| Live protection | Exchange-side protection/reconciliation where configured |
| Explainability | Trade explanations, counterfactuals and attention-based XAI |
| Learning | Online RL/ML feedback and continual-learning worker |
| Backtesting | Lightweight historical runner and richer L2/futures replay path |
| Operations | Health/metrics endpoints, structured JSON logging, Docker Compose |
| Deployment | GitHub Actions CI/CD with Docker and SSH-based VPS deployment |

[↑ Back to Index](#-interactive-index)

## Quick start

For a workstation-only setup with portable `./data` storage and no VPS credentials, follow the [VPS-free local development guide](docs/LOCAL_DEVELOPMENT.md).

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
docker compose up -d redis clickhouse clickhouse-init neo4j
```

The paper runtime's documented local service set includes Redis, ClickHouse initialization and Neo4j. Wait for the selected services to become healthy before starting the runner; do not omit a declared dependency without a separately tested configuration. This service-backed setup is resource-intensive: review the [local resource profiles](docs/LOCAL_DEVELOPMENT.md#21-choose-a-local-resource-profile) first. A 4 GiB workstation should use the frontend/sample workspace and focused unit tests rather than the full paper stack.

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

The canonical, step-by-step deployment procedure is maintained in the [AITOS Installation & Deployment Guide](INSTALLATION.md#method-2--deploy-the-aitos-runtime-on-a-vps). It covers VPS prerequisites, persistent data-disk preparation, GitHub Actions environment/secrets, immutable image deployment, health checks, maintenance and rollback.

For desktop installation and connecting the read-only console to a VPS over SSH, see [Method 1](INSTALLATION.md#method-1--install-the-desktop-operator-console).

[↑ Back to Index](#-interactive-index)

## CI/CD

GitHub Actions provides the Python 3.10–3.12 backend matrix, Redis/ClickHouse/Neo4j-backed integration coverage, formatting, critical Flake8 checks, Bandit/dependency scanning and Docker/Compose validation. Separate workflows validate the React console, native Windows/Linux session-manager packages, and the cross-platform Tauri shell. Relevant desktop workflows also run after matching changes reach `main`; Tauri/native artifacts remain unsigned test candidates. VPS CD is separately guarded by the repository variable `AITOS_VPS_ENABLED=true`; keep it unset while VPS deployment is paused. A successful CI run is not runtime, exchange, security-certification or live-readiness evidence.

[↑ Back to Index](#-interactive-index)

## Operations and troubleshooting

### Check service state

```bash
docker compose ps --all
```

### Inspect logs

```bash
docker compose logs --tail=200 aitos-paper
docker compose logs --tail=200 redis
docker compose logs --tail=200 clickhouse
docker compose logs --tail=200 neo4j
```

### Health endpoints

```bash
curl http://localhost:8090/health
curl http://localhost:8090/metrics
```

### Redis connectivity

```bash
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" PING
```

### ClickHouse connectivity

```bash
docker compose exec clickhouse clickhouse-client --query 'SELECT 1'
```

### Disk usage

```bash
df -hT /mnt/aitos-data
du -sh /mnt/aitos-data/*
```

Do not delete Redis, ClickHouse or Neo4j files manually while the corresponding service is running.

[↑ Back to Index](#-interactive-index)

## Storage and maintenance

Storage maintenance is profile/service based and should be run deliberately on production systems. Monitor the dedicated data disk before it approaches capacity.

[↑ Back to Index](#-interactive-index)

## Project structure

```text
aitos/
├── aitos/                 # Python trading, market-data, risk and learning platform
├── desktop/               # React/TypeScript console and Tauri shell
├── native/                # Windows/Linux packaging and shared desktop sidecar
├── docs/                  # User, operator, security and integration guides
├── engineering/           # Architecture decisions, audits and verification evidence
├── scripts/
├── tests/
├── .github/workflows/     # CI, desktop validation and guarded deployment
├── docker-compose.yml
├── Dockerfile
├── requirements*.txt
├── pyproject.toml
├── .env.example
└── README.md
```

[↑ Back to Index](#-interactive-index)

## Safety boundaries

AITOS is a trading system, not a guarantee of profit. Paper trading and testnet should be used before live execution. Live execution requires deliberate configuration, valid credentials, risk limits and human approval where configured.


### Execution integrity and recovery status

The canonical live runner now uses confirmed terminal fills, durable market-order
intent quarantine, actual reduce-only exits, write-through lifecycle application,
account snapshot checks and single-runtime capital ownership. Current Binance
conditional protection uses the separate algo-order API; old persisted order IDs
retain compatibility. These changes are not a production-readiness or live-activation
claim. Protective pre-submit recovery, service-backed fault evidence, supported
broker integrations, paper soak and latency gates remain open. See
[Execution recovery operations](docs/EXECUTION_RECOVERY.md) and the
[dated implementation evidence](engineering/EXECUTION_RECOVERY_IMPLEMENTATION_2026-11-07.md).

## Paper shadow and Windows console preparation

Default-off paper runner mirroring and Windows x64 native vault/installer source
are documented in [implementation evidence](engineering/WINDOWS_PAPER_INTEGRATION_2026-11-06.md)
and [Windows setup](native/windows/README.md). This is not a production journal
cutover or an available signed release. Actual service/Windows validation and
[independent review](engineering/PAPER_NATIVE_REVIEW_BUNDLE.md) remain gates.

## Offline recorded-account reconciliation

Use `python -m aitos.exchange.binance_account_evidence expected-schema2.json recorded-bundle.json`
for read-only exact comparison of independently prepared expected values against
recorded Binance-shaped paper/testnet responses. No account requests/credentials
or financial mutations. See [contract and verification](engineering/BINANCE_ACCOUNT_EVIDENCE_2026-11-06.md)
for schema, supported subset, pagination limitations and pending real-provider gates.

## Historical hosted Windows and service validation (2026-11-07)

The [remote validation evidence](engineering/REMOTE_VALIDATION_EVIDENCE.md)
records Windows vault/build/install/network/uninstall and disposable service
restart/soak checks for the exact older source SHAs listed in that report. Those
runs produced unsigned test candidates, not signed production releases. The
report's statement that no main merge had occurred describes the state at that
time only; it must not be read as the current repository status or as validation
of today's `main` head. Re-run the relevant workflows on the exact release
candidate SHA before relying on the evidence.

## Historical integration evidence

The older arena-branch integration review and its hosted workflow results are
historical evidence for the exact source SHAs recorded in those reports. They do
not certify the current `main` head, the merged Spot/architecture/compute changes,
or a production runtime. Use the [Architecture Evolution Program](engineering/ARCHITECTURE_EVOLUTION_PROGRAM.md)
and the [current system audit](engineering/SYSTEM_AUDIT_2026-11-20.md) for the
present acceptance gates.
