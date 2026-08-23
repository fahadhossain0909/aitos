<p align="center">
  <img src="assets/banner.svg" alt="AITOS — AI Trading Operating System" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/docker-compose%20ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker Compose ready">
  <img src="https://img.shields.io/badge/exchange-Binance%20USDT--M%20Futures-F0B90B?style=flat-square" alt="Binance USDT-M Futures">
  <img src="https://img.shields.io/badge/status-active%20development-blue?style=flat-square" alt="Active development">
</p>

# AITOS (ProjectAlpha)

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

The repository currently contains substantially more than the old README's `291 tests` claim; the old badge/count has intentionally been removed from this merged README.

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
git clone https://github.com/fahadhossain0909/ProjectAlpha.git aitos
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
git clone https://github.com/fahadhossain0909/ProjectAlpha.git aitos
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
`deploy/aitos-paper.service` / `deploy/aitos-live.service` আছে — কিন্তু এগুলো venv ধরে লেখা (`ExecStart=/opt/aitos/.venv/bin/python3 ...`), Docker না। Compose-কে systemd দিয়ে supervise করতে চাইলে `ExecStart` বদলে `docker compose up` / `docker compose --profile live run --rm aitos-live` বসাতে হবে।

### 2.9 হার্ডওয়্যার সাইজিং
4 vCPU / 8GB RAM পুরো stack (Redis + ClickHouse + Neo4j + app) স্বাচ্ছন্দ্যে চালায়। ClickHouse আর Neo4j-ই আসল RAM ভোক্তা, Python app নিজে হালকা।

---

## 3. Backtest

দুইটা mode:

### 3.1 Lightweight / deterministic (OHLCV বা generic price-event)
```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.cli \
  --source clickhouse --symbol BTCUSDT --table ohlcv --timeframe 15m \
  --start 2026-01-01T00:00:00+00:00 --end 2026-06-01T00:00:00+00:00 \
  --persist-learning
```
বা ফাইল থেকে:
```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.cli \
  --data /data/events.jsonl --strategy aitos.backtest.cli:buy_and_hold \
  --initial-cash 10000 --fee-rate 0.0004
```

### 3.2 Full L2/futures replay (ProjectAlphaHistoricalRunner — order-flow, footprint, liquidity, auction, L2 execution, margin/liquidation simulation সহ)
```bash
docker compose --profile backtest run --rm aitos-backtest \
  python3 -m aitos.backtest.rich_cli \
  --source clickhouse --symbol BTCUSDT --tick-size 0.10 \
  --start 2026-01-01T00:00:00Z --end 2026-02-01T00:00:00Z \
  --initial-cash 10000 --fee-rate 0.0004 --slippage-bps 1 --leverage 1
```
নিজের strategy লাগাতে: `--decision-strategy package.module:function` (contract: `def strategy(state) -> HistoricalDecision`)।

### 3.3 ডেটা সোর্স নিয়ম (গুরুত্বপূর্ণ)
1. প্রথমে ClickHouse-এ খুঁজবে (এটাই canonical, দীর্ঘমেয়াদী সোর্স)।
2. না পেলে external থেকে Parquet download হবে `/data/backtest`-এ (cap 20 GiB, পুরনোটা auto-evict হয়)।
3. Downloaded Parquet **কখনো** ClickHouse-এ ingest হয় না — এটা শুধু one-off replay cache।

### 3.4 Backtest চলাকালীন paper trading বন্ধ করার দরকার নেই
```
Paper trading:  RUNNING  -------------------------------->
Backtest:                 START ---- RUN ---- END
```
`aitos-backtest` container-এর কোনো public port নেই, file mount read-only, CPU/mem limited (`2.0` CPU / `3g` RAM)। **কখনো `down -v` দিয়ে backtest চালানোর জন্য infra বন্ধ করবেন না** — data volume মুছে যাবে।

### 3.5 Backtest → Learning pipeline
`--persist-learning` দিলে backtest-এর outcome shared Experience Store-এ যায়, `aitos-learning` worker সেগুলো replay করে `DeepValueRLScorer`-কে train করে। Candidate model সরাসরি production-এ যায় না — validation gate পার হতে হয়:

```
Candidate → canonical backtest → walk-forward validation → locked holdout
  → paper/shadow validation → governance gate → champion model
```

---

## 4. Paper Trading

### 4.1 চালু/বন্ধ (Docker Compose — VPS-এ default)
```bash
docker compose up -d aitos-paper        # শুরু
docker compose stop aitos-paper         # থামানো, data থাকবে
docker compose start aitos-paper        # আবার শুরু
docker compose logs -f aitos-paper      # লাইভ লগ
```

### 4.2 কোন symbol trade হয়
`run_paper_trading.py`-তে hardcoded: **BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT** — 15m timeframe, প্রতি 60 সেকেন্ডে এক scan cycle, starting equity $10,000 (simulated)।

### 4.3 Monitoring
```bash
curl http://localhost:8090/health    # JSON, module-wise status, 200/503
curl http://localhost:8090/metrics   # Prometheus format
```
`/metrics` data expose করে কিন্তু কোথাও push/alert হয় না (Prometheus/Grafana/PagerDuty wiring নেই) — এটা এখনো manual monitoring।

### 4.4 State কোথায় থাকে
Model checkpoint (`RL scorer`, `outcome classifier`, `attention explainer`) `aitos_model_data` Docker volume-এ (`/models`)। Trade/journal ClickHouse-এ (থাকলে)। ClickHouse না থাকলে persistence skip হয়, শুধু in-memory চলে — restart-এ history হারাবে।

---

## 5. Production / Live Trading

⚠️ **এইটা real order দেয়। প্রতিটা ধাপ মিস না করে পড়ুন।**

### 5.1 Pre-flight checklist
- [ ] `.env`-এ `BINANCE_API_KEY`/`BINANCE_API_SECRET` সেট আছে, key-তে **withdrawal permission নেই**
- [ ] `BINANCE_TESTNET=true` দিয়ে অন্তত একবার পুরো session চালিয়ে verify করেছেন
- [ ] `BINANCE_HEDGE_MODE` আপনার Binance account-এর actual position mode-এর সাথে মিলছে (না মিললে script নিজেই startup-এ বন্ধ হয়ে যাবে — এটা bug না, safety check)
- [ ] Paper trading-এ অন্তত কিছুদিন চালিয়ে risk engine/sector cap behave করা দেখেছেন
- [ ] VPS-এ terminal attach করার উপায় আছে (screen/tmux) — কারণ approval prompt interactive

### 5.2 চালু করা (foreground, attached — এটা `docker compose up -d`-এ auto-start হয় না)
```bash
docker compose --profile live run --rm aitos-live
```
Startup-এ script জিজ্ঞেস করবে:
1. আপনার নাম/identifier
2. হুবহু এই phrase টাইপ করতে হবে: `I APPROVE LIVE TRADING`

এই identifier-ই session-এর প্রতিটা trade-এর `approved_by` হয়ে journal-এ থাকবে। ভুল phrase টাইপ করলে বা Ctrl-C দিলে script `sys.exit(1)` করে বন্ধ হয়ে যায় — trade শুরুই হয় না।

### 5.3 systemd-এর সাথে টানাপোড়েন (জানা সমস্যা, ডকুমেন্টেড)
`deploy/aitos-live.service` নিজেই স্বীকার করে: systemd-এর কোনো attached terminal থাকে না, তাই এই interactive prompt-এ hang করবে। দুইটা বাস্তব সমাধান:
1. systemd না, terminal-এ (screen/tmux) ম্যানুয়ালি চালান, অথবা
2. `confirm_live_trading`-কে non-interactive pre-approval token-ভিত্তিক gate দিয়ে replace করুন (এটা এখনো বানানো হয়নি — README-এর "Next steps"-এও লেখা আছে)।

`Restart=no` ইচ্ছাকৃত — real-money process crash করলে human দেখবে, systemd silently restart করবে না।

### 5.4 চালু হওয়ার পর যা automatic ভাবে ঘটে
- শুধু **BTCUSDT, ETHUSDT** trade হয় (paper-এর ৪টার চেয়ে কম — narrower live universe)
- `use_exchange_side_stops=True` সবসময় — real `STOP_MARKET`/`TAKE_PROFIT_MARKET` order Binance-এ বসে
- প্রতি scan cycle-এর পর `ReconciliationScheduler.run_once()` চলে (exchange-এ resting order-এর status verify করে internal state-এর সাথে sync রাখে) — এর পাশাপাশি background-এ প্রতি 30 সেকেন্ডে independent reconciliation loop-ও চলে
- প্রতিটা production trade `AIKernel.enforce_governance`-এর মধ্য দিয়ে যায় (human approval ছাড়া কোনো `is_production=True` action pass হয় না)
- Exchange precision (`/fapi/v1/exchangeInfo`) startup-এ একবার load হয় — **periodic refresh নেই**, তাই Binance যদি মাঝপথে symbol filter বদলায়, নতুন করে restart না করলে সেটা ধরবে না

### 5.5 Monitoring
```bash
curl http://localhost:8091/health
docker compose logs -f aitos-live
```

### 5.6 এখনো manual/না-থাকা কিছু জিনিস (production-এ চালানোর আগে জানা দরকার)
- **Drawdown persistence নেই** — `LivePortfolioTracker`-এর peak equity শুধু in-memory; script restart হলে drawdown tracking রিসেট হয়ে যায়, historical peak হারায়।
- **Per-symbol leverage auto-set হয় না** — `set_leverage()` function আছে কিন্তু কোথাও automatically call হয় না; Binance account-এ leverage আগে থেকে ম্যানুয়ালি সেট করে রাখতে হবে।
- **কোনো alerting নেই** — `/metrics` শুধু data expose করে; কোনো paging/notification নেই, log/health endpoint নিয়মিত নিজে check করতে হবে বা নিজে Prometheus+Alertmanager বসাতে হবে।

### 5.7 বন্ধ করা
Foreground-এ Ctrl-C দিলে graceful shutdown হয় (open connection/health server ঠিকভাবে বন্ধ হয়)। `docker compose --profile live run --rm` যেহেতু one-off container, `docker compose down` এখানে প্রযোজ্য না — Ctrl-C-ই সঠিক উপায়।

---

## 6. Continual Learning Worker (`aitos-learning`)

`docker compose up -d --build`-এর সাথেই এটা auto-start হয় (profile-gated না)। এটা `run_continual_learning.py` চালায়, প্রতি 60 সেকেন্ডে (env: `LEARNING_POLL_SECONDS`) ClickHouse থেকে **শুধু backtest experience** replay করে `DeepValueRLScorer`-কে train করে — paper/live experience এখানে replay হয় **না**, কারণ সেগুলো `RLFeedbackLoop` real-time-এই train করে ফেলে (duplicate update এড়াতে ইচ্ছাকৃত ডিজাইন)।

একবারই চালাতে চাইলে:
```bash
docker compose run --rm aitos-learning python3 run_continual_learning.py --once
```
Resource limit: `0.5` CPU / `512m` RAM — paper trading-এর সাথে aggressively compete করে না।

---

## 7. Storage Maintenance (`aitos-storage-maintenance`)

প্রতিদিন একবার (env: `STORAGE_MAINTENANCE_INTERVAL_SECONDS=86400`) চলে, ClickHouse storage বাজেটের ভেতর রাখে:
- বাজেট: **100 GiB**, cleanup target **90 GiB**
- Retention ladder: `90 → 30 → 15 → 10 → 7` দিন (budget ছাড়ালে ছোট করে)
- **কখনো মুছবে না**: trade, order/fill, position, decision/journal, risk record, model version, experience/replay data
- **মুছতে পারে**: `order_book_snapshots`, `order_book_updates`, `market_ohlcv` (এগুলো আবার download করা যায়)
- Backtest download cache আলাদাভাবে 20 GiB-তে capped, oldest first evict হয়

Dry-run (কিছু না মুছে শুধু কী মুছবে দেখতে):
```bash
STORAGE_MAINTENANCE_DRY_RUN=true docker compose run --rm aitos-storage-maintenance
```

---

## 8. Troubleshooting Cheat-Sheet

| সমস্যা | কমান্ড |
|---|---|
| Container status | `docker compose ps` / `docker compose ps --all` |
| ClickHouse বেঁচে আছে কিনা | `docker exec aitos-clickhouse clickhouse-client --query 'SELECT 1'` |
| Redis বেঁচে আছে কিনা | `docker exec aitos-redis redis-cli ping` |
| সাম্প্রতিক error খোঁজা | `docker compose logs --no-color --tail=500 \| grep -Ei 'error\|exception\|traceback\|fatal\|oom'` |
| Health | `curl http://127.0.0.1:8090/health` (paper) / `:8091` (live) |
| Container restart count | `docker inspect --format '{{.Name}} restart={{.RestartCount}}' <container>` |
| ClickHouse row counts (কোনো credential print ছাড়া) | `docker exec aitos-clickhouse clickhouse-client --query "SELECT database, table, total_rows FROM system.tables WHERE database NOT IN ('system','INFORMATION_SCHEMA','information_schema')"` |

উপরের কমান্ডগুলোই `.github/workflows/production-audit.yml`-এর read-only VPS audit script যা করে — চাইলে GitHub Actions থেকে `workflow_dispatch` দিয়েও চালাতে পারেন (Actions ট্যাব → Production VPS Audit → Run workflow), report artifact হিসেবে ৭ দিন থাকবে।

---

## 9. Audit Findings — Documentation ও CI/CD hygiene (এই manual বানানোর সময় পাওয়া)

এগুলো code bug না, কিন্তু ঠিক করলে maintenance সহজ হবে:

1. **তিনটা README** — `README.md` (current, 820 lines), `README1.md` (আগের snapshot, 722 lines), `README2.md` (আলাদা generic CI/CD-focused README, Python 3.9+ লেখা যেখানে আসল Dockerfile 3.12 ব্যবহার করে)। একসাথে রাখলে কোনটা authoritative বোঝা কঠিন — `README1.md`/`README2.md` মুছে ফেলা বা `docs/`-এ archive করা ভালো।
2. **`SETUP_GUIDE.md` ভুল secret list দেখায়** — `DATABASE_URL`, `API_KEY`, `SECRET_KEY`, `DEBUG` এই প্রজেক্টের কোথাও ব্যবহৃত হয় না (generic Django template থেকে রয়ে গেছে); আসল দরকারি secret হলো `REDIS_PASSWORD`, `CLICKHOUSE_PASSWORD`, `NEO4J_PASSWORD`, `BINANCE_API_KEY/SECRET`, `DEPLOY_HOST/USER/SSH_KEY`।
3. **`cd.yml`-এর auto-generated `.env` অসম্পূর্ণ** — উপরে ২.৩-এ যা লিখেছি, একই কারণে: real infra credential গুলো deploy workflow-তে লেখাই হয় না।
4. **`ci.yml`-এ `decision_fusion.py` নিয়ে ৪ বার ডুপ্লিকেট diagnostic step** (sha256sum/hash-object/AST-parse) — মনে হচ্ছে কোনো caching/import bug ডিবাগ করার জন্য যোগ করা হয়েছিল, এখন পরিষ্কার করা যায়।
5. **Lint ও security scan ব্যর্থ হলেও CI পাস করে** — `black`, `isort`, `bandit`, `safety` সবগুলোতে `continue-on-error: true`, ফলে এগুলো আসলে কিছুই block করে না, শুধু info-only।
6. GitHub-এর file listing-এ `.env.example1` আর `.github1/workflows`-এর মতো নাম দেখা গিয়েছিল কিন্তু বর্তমান `main`-এর fresh clone-এ এগুলো নেই — সম্ভবত cached/পুরনো view অথবা সম্প্রতি মুছে ফেলা হয়েছে, একবার GitHub-এ সরাসরি চেক করে নিশ্চিত হওয়া ভালো।

## 10. ভালো খবর — আগের incident-টা ঠিক হয়ে গেছে

আগে যে EMERGENCY STOP হয়েছিল (BNBUSDT sector mapping না থাকায় "unclassified" bucket-এ ~248% exposure), সেটা কোডে **কনফার্ম করে ঠিক করা** পাওয়া গেছে: `aitos/risk/sector.py`-তে এখন BNBUSDT স্পষ্টভাবে `exchange-token` sector-এ ম্যাপ করা, এবং যেকোনো নতুন/অচেনা symbol আর unbounded "unclassified" bucket-এ না গিয়ে ডিফল্টভাবে `other` sector-এ পড়ে, যেটা sector cap-এর আওতায় থাকে। নতুন symbol Scanner-এ যোগ করার আগে `SYMBOL_SECTORS`-এ entry দেওয়া ভালো অভ্যাস, কিন্তু ভুলে গেলেও risk engine আর silently unbounded থাকবে না।
