<p align="center">
  <img src="assets/banner.svg" alt="AITOS — AI Trading Operating System" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/docker-compose%20ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker Compose ready">
  <img src="https://img.shields.io/badge/exchange-Binance%20USDT--M%20Futures-F0B90B?style=flat-square" alt="Binance USDT-M Futures">
  <img src="https://img.shields.io/badge/status-active%20development-blue?style=flat-square" alt="Active development">
</p>

# AITOS

AITOS is an AI Trading Operating System for Binance USDT-M Futures, with event-driven architecture, Redis Streams, ClickHouse persistence, optional Neo4j knowledge graph, paper trading, guarded live execution, backtesting, continual learning, risk controls, XAI/journaling, and Docker-based deployment.

> **Authoritative documentation:** this README combines the project overview with the audited Operations Manual. Operational claims below are based on the actual repository code/configuration rather than stale README claims.

## Quick start

### Paper trading

```bash
docker compose up -d redis clickhouse neo4j
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 run_paper_trading.py
```

Health: `http://localhost:8090/health`

### Test suite

```bash
PYTHONPATH=. pytest -v
```

The repository currently contains substantially more than the old `291 tests` claim; the old badge/count has intentionally been removed from this merged README.

## Architecture

```text
Binance REST/WebSocket
        │
        ▼
Redis Streams Event Bus
        │
        ├── Risk Engine ── Opportunity Scanner ── Trade Lifecycle
        │                                      │
        │                                      ▼
        │                              Paper / Live Execution
        │                                      │
        ├── Journal / XAI ◄───────────────────┘
        ├── RL feedback / continual learning
        └── Neo4j Knowledge Graph (optional)

ClickHouse = canonical long-term market history, journal and learning experience
Redis      = real-time Event Bus
Neo4j      = optional knowledge graph
```

## Core capabilities

- Event-driven module contracts and Redis Streams Event Bus with consumer groups, DLQ, replay and request/reply.
- Binance USDT-M Futures REST/WebSocket market-data adapter with rate limiting and reconnect handling.
- Risk scoring, circuit breaker, hard/soft limits, sector exposure guard, position sizing and adaptive leverage.
- Opportunity scanning using trend, volatility, CVD/order-flow, auction context, liquidity, funding, open interest, lead-lag and RL confidence.
- Paper trading and separately guarded live trading.
- Exchange-side stop-loss/take-profit orders and automatic reconciliation for live execution.
- ExchangeInfo-based quantity/price filters and optional Binance hedge-mode support.
- ClickHouse market-data/journal persistence, optional Neo4j knowledge graph, continual-learning worker and storage-maintenance worker.
- XAI trade explanations, counterfactuals, online outcome classification and attention-based explanations.
- Health/metrics endpoints, structured JSON logging, Docker Compose and GitHub Actions CI/CD.
- Deterministic/lightweight and full L2/futures replay backtesting.

## Important safety note

`run_live_trading.py` places real orders. Live execution requires Binance credentials, explicit human session approval, matching hedge-mode configuration, and testnet verification first. Never grant withdrawal permission to the API key.

---

# Operations Manual

## 0. প্রজেক্ট এক নজরে

চারটা runnable entrypoint:

| Script | কাজ | Health port |
|---|---|---|
| `run_paper_trading.py` | Live Binance data, paper-traded (real order না) | 8090 |
| `run_live_trading.py` | Real order, Binance testnet/mainnet | 8091 |
| `run_continual_learning.py` | Backtest experience থেকে RL model train করে (background worker) | — |
| `python3 -m aitos.backtest.cli` / `rich_cli` | Historical backtest (দুই mode) | — |

Data layer: **Redis** (Event Bus, real-time), **ClickHouse** (দীর্ঘমেয়াদী market history + journal + learning experience), **Neo4j** (knowledge graph, optional)।

---

## 1. PC (Local Development) Setup

### 1.1 প্রয়োজনীয় জিনিস
- Python 3.12 (3.10/3.11-ও CI-তে test হয়, কিন্তু Docker image 3.12 ব্যবহার করে)
- Docker + Docker Compose plugin
- Git

### 1.2 Clone ও venv
```bash
git clone https://github.com/fahadhossain0909/aitos.git aitos
cd aitos
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
শুধু backtest নিয়ে কাজ করলে হালকা dependency set:
```bash
pip install -r requirements-backtest.txt
```

### 1.3 Infra চালু করা (শুধু dependencies, app না)
```bash
docker compose up -d redis clickhouse neo4j
```
Redis **required** (Event Bus)। ClickHouse/Neo4j optional — না থাকলেও `run_paper_trading.py` চলবে, শুধু persistence/knowledge-graph অংশ skip হবে (log-এ warning আসবে)।

### 1.4 Config
```bash
cp .env.example .env
```
Local/paper-এর জন্য default values ঠিক আছে — Binance credential লাগবে না।

### 1.5 Test suite চালানো
```bash
PYTHONPATH=. pytest -v
```
এটা `fakeredis` আর mock দিয়ে চলে — Docker চালু না থাকলেও কাজ করবে, দ্রুত। বর্তমানে repo-তে backtest/learning/storage মডিউলসহ **~300+ test file/function** আছে (README-এ লেখা "291 tests" এখন পুরনো সংখ্যা — কোডবেস এরপর অনেক বেড়েছে: backtest engine, continual learning, order-flow/footprint/AMT engine, sector-exposure guard যোগ হয়েছে)।

Backtest-specific test শুধু:
```bash
python -m pytest -q tests/backtest
```

### 1.6 Local paper trading (venv-এ সরাসরি, Docker ছাড়া app)
```bash
python3 run_paper_trading.py
```
এটা live Binance public data নিয়ে paper trade করবে, Ctrl-C-তে graceful shutdown। `http://localhost:8090/health` চেক করুন।

---

## 2. VPS Setup (Production Deployment)

### 2.1 Docker install
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker compose version
```
Oracle Cloud ARM (Ampere A1)-এ automatic arm64 build হয়ে যাবে — আলাদা কিছু করা লাগে না। কিন্তু `shap`-এর jonyalচcompiled wheel না থাকায় ARM-এ প্রথম `docker compose build` অনেক সময় নেবে (Dockerfile-এ তাই `build-essential`/`gcc`/`g++` রাখা আছে)।

### 2.2 Clone
```bash
git clone https://github.com/fahadhossain0909/aitos.git aitos
cd aitos
```

### 2.3 Production `.env` — অবশ্যই বদলাতে হবে এই মানগুলো
`.env.example` কপি করে **সব secret** নিজে generate করুন, default/blank রাখবেন না:

```bash
cp .env.example .env
openssl rand -hex 32   # প্রতিটা secret-এর জন্য আলাদা করে চালান
```

| Variable | কেন জরুরি |
|---|---|
| `REDIS_PASSWORD` | blank রাখলে Redis unauthenticated থাকে |
| `CLICKHOUSE_PASSWORD` | default blank — production-এ অবশ্যই সেট করুন |
| `NEO4J_PASSWORD` | default `changeme` — অবশ্যই বদলান |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | শুধু live trading-এর জন্য; **withdrawal permission কখনো দেবেন না** |
| `BINANCE_TESTNET` | live trading যাচাই করা পর্যন্ত `true` রাখুন |
| `BINANCE_HEDGE_MODE` | আপনার Binance account-এর position mode-এর সাথে মিলতে হবে, নাহলে `run_live_trading.py` startup-এই বন্ধ হয়ে যাবে |

⚠️ **Audit finding**: `SETUP_GUIDE.md` এবং `.github/workflows/cd.yml`-এ যে secret list (`DATABASE_URL`, `API_KEY`, `SECRET_KEY`, `DEBUG`) দেখানো আছে, সেগুলো generic Django-স্টাইল template থেকে রয়ে গেছে — এই প্রজেক্টের actual কোড এগুলোর একটাও ব্যবহার করে না। CD workflow-এর "Create .env file on deployment server" ধাপটি `REDIS_PASSWORD`, `CLICKHOUSE_PASSWORD`, `NEO4J_PASSWORD`, `BINANCE_*` — এগুলোর কোনোটাই লেখে না, ফলে GitHub Actions দিয়ে auto-deploy করলে VPS-এর `.env`-এ real credential গুলো **অনুপস্থিত** থাকবে। যতক্ষণ `cd.yml` ঠিক না হচ্ছে, ততক্ষণ VPS-এ SSH করে `.env` ম্যানুয়ালি বসাতে হবে অথবা `cd.yml`-এর `.env` block-টা settings.py-র actual variable list দিয়ে rewrite করা দরকার।

### 2.4 চালু করা
```bash
docker compose up -d --build
```
এটা build করবে, Redis/ClickHouse/Neo4j healthcheck-এর জন্য অপেক্ষা করবে, তারপর `aitos-paper` অটো-স্টার্ট হবে (কোনো profile gate নেই)। `aitos-learning` আর `aitos-storage-maintenance`-ও একসাথে চালু হয়ে যাবে (এদেরও কোনো profile নেই — শুধু `backtest` আর `live` profile-gated)।

যাচাই:
```bash
docker compose ps
docker compose logs -f aitos-paper
curl http://localhost:8090/health
```

### 2.5 Firewall / নেটওয়ার্ক নিরাপত্তা
`docker-compose.yml`-এ সব port (`6379`, `8123`, `9000`, `7474`, `7687`, `8090`, `8091`) ইতিমধ্যে `127.0.0.1:` তে bind করা — বাইরে থেকে expose হয় না, এটা ভালো ডিফল্ট। VPS নিজের firewall (ufw/iptables) থেকেও শুধু SSH port খোলা রাখুন। Health/metrics দেখতে চাইলে SSH tunnel ব্যবহার করুন:
```bash
ssh -L 8090:localhost:8090 your-vps
```

### 2.6 আপডেট করা
```bash
git pull
docker compose up -d --build
```

### 2.7 বন্ধ করা
```bash
docker compose down       # container বন্ধ, volume (data) থাকবে
docker compose down -v    # volume-ও মুছে যাবে — এটা irreversible, রুটিন কাজে কখনো না
```

### 2.8 systemd দিয়ে (Docker Compose-এর বদলে চাইলে)
`deploy/aitos-paper.service` / `deploy/aitos-live.service` আছে — কিন্তু এগুলো venv ধরে লেখা (`ExecStart=/opt/aitos/.venv/bin/python3 ...`), Docker না। Compose-কে systemd দিয়ে supervise করতে চাইলে আলাদা unit লিখতে হবে।

---

## 3. Backtesting

### 3.1 Lightweight deterministic replay
```bash
python3 -m aitos.backtest.cli --help
```
এই mode event replay করে strategy সিদ্ধান্তের উপর execution simulate করে।

### 3.2 Full L2/futures replay (`aitos.backtest` historical runner — order-flow, footprint, liquidity, auction, L2 execution, margin/liquidation simulation সহ)
এই mode-এর runner historical market state, L2 book, queue lifecycle, fees, funding এবং futures margin/liquidation একসাথে simulate করে।

---

## 4. Paper Trading

Paper mode real Binance market data consume করে কিন্তু real order পাঠায় না। এটি production deployment-এর default trading mode এবং live execution-এর আগে verification-এর জন্য ব্যবহার করা উচিত।

---

## 5. Live Trading Safety

Live mode চালু করার আগে:

1. Binance API key-তে **withdrawal permission বন্ধ** রাখুন।
2. `BINANCE_TESTNET=true` দিয়ে testnet যাচাই করুন।
3. `BINANCE_HEDGE_MODE` account configuration-এর সাথে মিলিয়ে নিন।
4. Risk limits ও circuit breaker যাচাই করুন।
5. Startup logs এবং `/health` endpoint যাচাই করুন।
6. তারপরই mainnet credentials ব্যবহার করুন।

---

## 6. Continual Learning

`run_continual_learning.py` historical/backtest experience থেকে RL model training worker চালায়। Training output ও learning experience ClickHouse-এ persistence করা হয়। Production-এ learning worker-কে live execution থেকে আলাদা করে monitor করা উচিত।

---

## 7. Storage Maintenance

Storage-maintenance worker ClickHouse/Redis-related housekeeping ও retention tasks পরিচালনা করে। VPS disk/RAM usage পর্যবেক্ষণ করুন এবং log/data growth-এর জন্য retention policy প্রয়োগ করুন।

---

## 8. CI/CD

GitHub Actions workflows:

- `.github/workflows/ci.yml` — tests/checks
- `.github/workflows/cd.yml` — deployment
- `.github/workflows/backtest.yml` — backtest validation
- `.github/workflows/production-audit.yml` — production audit

Deployment environment এবং secrets repository-এর actual production configuration-এর সাথে মিলিয়ে রাখতে হবে। বিশেষ করে `REDIS_PASSWORD`, `CLICKHOUSE_PASSWORD`, `NEO4J_PASSWORD` এবং deployment SSH credentials GitHub Secrets/Environment থেকে আসতে হবে।

---

## 9. Troubleshooting

### Redis
```bash
docker compose logs redis
docker compose ps redis
```

### ClickHouse
```bash
docker compose logs clickhouse
docker compose ps clickhouse
```

### Neo4j
```bash
docker compose logs neo4j
docker compose ps neo4j
```

### AITOS paper service
```bash
docker compose logs -f aitos-paper
curl http://localhost:8090/health
```

### Full container/resource status
```bash
docker compose ps
docker stats --no-stream
```

---

## 10. Naming / Migration

The GitHub repository is now `fahadhossain0909/aitos`. Use `aitos` for all new documentation, deployment commands, URLs, Docker service references, CI/CD configuration, and scripts. The former repository name must not be reintroduced into active configuration or documentation.
