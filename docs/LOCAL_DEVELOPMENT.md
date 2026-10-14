# Local Development (VPS-free)

AITOS can be developed and paper-tested on a local workstation. No VPS, SSH
credentials, or deployment secrets are required for the normal local workflow.

## 1. Configure local storage

Copy `.env.example` to `.env`. The example uses repository-relative storage
under `./data`, so Docker Desktop, Linux, and macOS do not need the old
`/mnt/aitos-data` mount point.

You can move the checkout or data directory without editing Python source:

- `AITOS_HOME`: optional absolute path to the checkout root.
- `AITOS_DATA_ROOT`: root for Redis, ClickHouse, Neo4j, backtest, and artifact data.
- `MODEL_DATA_DIR`: model checkpoints and online-learning state; relative paths
  resolve from the checkout root.
- `AITOS_ENV_FILE`: optional path to an alternate environment file.

The native Python runtime defaults model state to `data/models`. Inside the
provided Docker image, model state defaults to the mounted `/models` volume.
Explicit `MODEL_DATA_DIR` always takes precedence.

## 2. Start local data services

Install Docker Desktop (Windows/macOS) or Docker Engine + Compose (Linux), then
run from the repository root:

```bash
docker compose config
docker compose up -d redis clickhouse clickhouse-init neo4j
```

Check service status with `docker compose ps`. The default published ports are
bound to localhost only. Storage maintenance defaults to dry-run mode so a local
full-stack run cannot automatically prune local data; explicitly set
`STORAGE_MAINTENANCE_DRY_RUN=false` only when you intentionally want cleanup.

## 3. Install Python dependencies

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Python 3.10 is the minimum supported runtime; Python 3.11 is a convenient
default for local development.

## 4. Validate before running

```bash
python -m pytest -q
```

For a local paper-trading run (simulated order execution, not live orders):

```bash
python run_paper_trading.py
```

Stop it with Ctrl+C. Do not run `run_live_trading.py` unless you intentionally
configure and approve live execution; paper mode should be used for local
validation first.

## 5. VPS workflows are paused

Deployment, VPS recovery/reset, scheduled server checks, and SSH-based forensic
workflows have a job guard. They are skipped unless the repository variable
`AITOS_VPS_ENABLED` is explicitly set to the string `true`. Leave it unset
while working locally. Workflow files are retained and can be re-enabled later
when a VPS is available.

CI, unit tests, lint/format checks, and local backtests remain available without
that variable.
