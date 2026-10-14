#!/bin/bash
set -euo pipefail
AITOS_DIR="${AITOS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_ROOT="${AITOS_DATA_ROOT:-$AITOS_DIR/data}"
REDIS_DATA_DIR="${REDIS_DATA_DIR:-$DATA_ROOT/eventbus/redis}"
if [ -f "$AITOS_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$AITOS_DIR/.env"
  set +a
fi
mkdir -p "$REDIS_DATA_DIR/live"
redis-server --daemonize yes --requirepass "${REDIS_PASSWORD:?Set REDIS_PASSWORD in .env or environment}" --port "${REDIS_PORT:-6379}" --dir "$REDIS_DATA_DIR/live" --appendonly yes --maxmemory "${REDIS_MAXMEMORY:-2gb}" --maxmemory-policy "${REDIS_MAXMEMORY_POLICY:-noeviction}"
# ClickHouse and Neo4j are optional for development; start them separately if installed.
echo "AITOS Redis started. Data root: $DATA_ROOT"
