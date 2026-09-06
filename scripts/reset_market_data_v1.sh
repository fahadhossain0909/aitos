#!/usr/bin/env bash
# Intentionally destructive development reset for the v1 market-data plane.
# Run only when AITOS is stopped and disposable data has been approved.
set -euo pipefail

: "${AITOS_REDIS_CONTAINER:=aitos-redis}"
: "${AITOS_CLICKHOUSE_CONTAINER:=aitos-clickhouse}"
: "${AITOS_NEO4J_CONTAINER:=aitos-neo4j}"
: "${AITOS_CLICKHOUSE_DATABASE:=aitos}"

confirm="${AITOS_CONFIRM_RESET:-}"
if [[ "$confirm" != "YES" ]]; then
  echo "Refusing destructive reset. Set AITOS_CONFIRM_RESET=YES explicitly." >&2
  exit 2
fi

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

echo "=== AITOS v1 market-data reset ==="
echo "redis=$AITOS_REDIS_CONTAINER clickhouse=$AITOS_CLICKHOUSE_CONTAINER neo4j=$AITOS_NEO4J_CONTAINER"

# Redis: remove application streams and consumer-group state, but do not touch
# unrelated Redis keys unless the deployment is explicitly using a dedicated DB.
docker exec "$AITOS_REDIS_CONTAINER" redis-cli --raw --scan --pattern 'stream:*' |
while IFS= read -r key; do
  [[ -n "$key" ]] && docker exec "$AITOS_REDIS_CONTAINER" redis-cli UNLINK "$key" >/dev/null
done

echo "Redis market streams removed."

# ClickHouse: drop only the development database. Production deployments must
# point AITOS_CLICKHOUSE_DATABASE at a dedicated disposable database.
docker exec "$AITOS_CLICKHOUSE_CONTAINER" clickhouse-client \
  --query="DROP DATABASE IF EXISTS ${AITOS_CLICKHOUSE_DATABASE}"

docker exec "$AITOS_CLICKHOUSE_CONTAINER" clickhouse-client \
  --query="CREATE DATABASE ${AITOS_CLICKHOUSE_DATABASE}"

echo "ClickHouse database recreated: $AITOS_CLICKHOUSE_DATABASE"

# Neo4j: clear the graph only when the configured database is disposable.
docker exec "$AITOS_NEO4J_CONTAINER" cypher-shell \
  'MATCH (n) DETACH DELETE n'
echo "Neo4j graph cleared."

echo "=== RESET COMPLETE ==="
