#!/usr/bin/env bash
# Intentionally destructive development reset for the v1 market-data plane.
# Run only when AITOS is stopped and disposable data has been approved.
# This script is invoked through bash by CD emergency recovery; executable mode is not required.
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
  "$AITOS_DATA_ROOT/eventbus/redis"
  "$AITOS_DATA_ROOT/databases/clickhouse"
  "$AITOS_DATA_ROOT/databases/neo4j"
  "$AITOS_DATA_ROOT/runtime/logs/neo4j"
)

# Emergency recovery must also handle an older/incomplete canonical layout.
# The normal bootstrap recreates the exact live/archive directory structure
# after this reset, so the reset operates on the verified database roots rather
# than requiring every child directory to exist already.
for path in "${DATA_PATHS[@]}"; do
  [[ "$path" == "$AITOS_DATA_ROOT/"* ]] || {
    echo "Refusing reset outside the canonical data root: $path" >&2
    exit 1
  }
  if [[ -e "$path" || -L "$path" ]]; then
    [[ -d "$path" && ! -L "$path" ]] || {
      echo "Refusing reset: expected database root is not a real directory: $path" >&2
      exit 1
    }
  else
    mkdir -p "$path"
  fi
done

# Never follow symlinks while clearing database contents.
for path in "${DATA_PATHS[@]}"; do
  find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
done
mkdir -p \
  "$AITOS_DATA_ROOT/eventbus/redis/live" \
  "$AITOS_DATA_ROOT/eventbus/redis/archive" \
  "$AITOS_DATA_ROOT/databases/clickhouse" \
  "$AITOS_DATA_ROOT/databases/neo4j" \
  "$AITOS_DATA_ROOT/runtime/logs/neo4j"

echo "=== AITOS v1 market-data reset ==="
echo "redis=$AITOS_REDIS_CONTAINER clickhouse=$AITOS_CLICKHOUSE_CONTAINER neo4j=$AITOS_NEO4J_CONTAINER"
echo "Verified database roots cleared and canonical layout recreated."

echo "=== RESET COMPLETE ==="
df -h "$AITOS_DATA_ROOT"
