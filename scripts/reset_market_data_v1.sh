#!/usr/bin/env bash
# Intentionally destructive development reset for the v1 market-data plane.
# Run only when AITOS is stopped and disposable data has been approved.
set -euo pipefail

: "${AITOS_REDIS_CONTAINER:=aitos-redis}"
: "${AITOS_CLICKHOUSE_CONTAINER:=aitos-clickhouse}"
: "${AITOS_NEO4J_CONTAINER:=aitos-neo4j}"
: "${AITOS_CLICKHOUSE_DATABASE:=aitos}"
: "${AITOS_DATA_ROOT:=/mnt/aitos-data}"

confirm="${AITOS_CONFIRM_RESET:-}"
if [[ "$confirm" != "YES" ]]; then
  echo "Refusing destructive reset. Set AITOS_CONFIRM_RESET=YES explicitly." >&2
  exit 2
fi

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

[[ "$AITOS_DATA_ROOT" = /mnt/aitos-data ]] || {
  echo "Refusing reset outside the canonical AITOS data root: $AITOS_DATA_ROOT" >&2
  exit 1
}
mountpoint -q "$AITOS_DATA_ROOT" || {
  echo "Refusing reset: $AITOS_DATA_ROOT is not a mounted filesystem" >&2
  exit 1
}

DATA_PATHS=(
  "$AITOS_DATA_ROOT/eventbus/redis/live"
  "$AITOS_DATA_ROOT/eventbus/redis/archive"
  "$AITOS_DATA_ROOT/databases/clickhouse"
  "$AITOS_DATA_ROOT/databases/neo4j"
  "$AITOS_DATA_ROOT/runtime/logs/neo4j"
)
for path in "${DATA_PATHS[@]}"; do
  [[ "$path" == "$AITOS_DATA_ROOT/"* && -d "$path" ]] || {
    echo "Refusing reset: expected database path is missing: $path" >&2
    exit 1
  }
done

echo "=== AITOS v1 market-data reset ==="
echo "redis=$AITOS_REDIS_CONTAINER clickhouse=$AITOS_CLICKHOUSE_CONTAINER neo4j=$AITOS_NEO4J_CONTAINER"

redis_running="$(docker inspect -f '{{.State.Running}}' "$AITOS_REDIS_CONTAINER" 2>/dev/null || echo false)"
clickhouse_running="$(docker inspect -f '{{.State.Running}}' "$AITOS_CLICKHOUSE_CONTAINER" 2>/dev/null || echo false)"
neo4j_running="$(docker inspect -f '{{.State.Running}}' "$AITOS_NEO4J_CONTAINER" 2>/dev/null || echo false)"

if [[ "$redis_running" = true && "$clickhouse_running" = true && "$neo4j_running" = true ]]; then
  docker exec "$AITOS_REDIS_CONTAINER" redis-cli --raw --scan --pattern 'stream:*' |
  while IFS= read -r key; do
    [[ -n "$key" ]] && docker exec "$AITOS_REDIS_CONTAINER" redis-cli UNLINK "$key" >/dev/null
done

  echo "Redis market streams removed."

  docker exec "$AITOS_CLICKHOUSE_CONTAINER" clickhouse-client \
    --query="DROP DATABASE IF EXISTS ${AITOS_CLICKHOUSE_DATABASE}"
  docker exec "$AITOS_CLICKHOUSE_CONTAINER" clickhouse-client \
    --query="CREATE DATABASE ${AITOS_CLICKHOUSE_DATABASE}"
  echo "ClickHouse database recreated: $AITOS_CLICKHOUSE_DATABASE"

  docker exec "$AITOS_NEO4J_CONTAINER" cypher-shell \
    'MATCH (n) DETACH DELETE n'
  echo "Neo4j graph cleared."
else
  echo "Database containers are not all running; performing an offline disposable-state reset."
  for path in "${DATA_PATHS[@]}"; do
    find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  done
  echo "Redis, ClickHouse, Neo4j and Neo4j logs were cleared from the verified data disk."
fi

echo "=== RESET COMPLETE ==="
df -h "$AITOS_DATA_ROOT"
