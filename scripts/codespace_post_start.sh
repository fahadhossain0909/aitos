#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[aitos] post-start: ensuring compose infra"
if command -v docker >/dev/null 2>&1; then
  docker compose up -d redis clickhouse clickhouse-init neo4j || true
  echo "[aitos] infra started (or already running)"
else
  echo "[aitos] docker not available yet"
fi
