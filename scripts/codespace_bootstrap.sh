#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[aitos] bootstrap: installing python deps"
python3 -m pip install --user -q -r requirements.txt

if [[ ! -f .env ]]; then
  echo "[aitos] creating .env from .env.example (codespace defaults)"
  cp .env.example .env
  {
    echo ""
    echo "# --- Codespace overrides ---"
    echo "AITOS_MODE=paper"
    echo "ENVIRONMENT=dev"
    echo "BINANCE_TESTNET=true"
    echo "REDIS_PASSWORD=local-development-only-change-this"
    echo "NEO4J_PASSWORD=local-development-only-change-this"
    echo "AITOS_DATA_ROOT=./data"
    echo "MODEL_DATA_DIR=./data/runtime/models"
  } >> .env
fi

mkdir -p data/runtime/models data/eventbus/redis data/databases/clickhouse data/databases/neo4j
echo "[aitos] bootstrap complete"
