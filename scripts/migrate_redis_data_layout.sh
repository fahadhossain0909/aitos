#!/bin/sh
set -eu
ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}/redis"
LIVE="$ROOT/live"
ARCHIVE="$ROOT/archive"
if [ ! -d "$ROOT" ]; then echo "Redis data root does not exist: $ROOT"; exit 0; fi
mkdir -p "$LIVE" "$ARCHIVE"
if command -v redis-cli >/dev/null 2>&1 && redis-cli -h "${REDIS_HOST:-127.0.0.1}" -p "${REDIS_PORT:-6379}" ping >/dev/null 2>&1; then
  echo "Redis is reachable; stop Redis before migrating its persistent files." >&2; exit 1
fi
for base in dump.rdb appendonly.aof appendonlydir; do
  item="$ROOT/$base"; [ -e "$item" ] || continue
  if [ -e "$LIVE/$base" ]; then echo "Refusing to overwrite existing live path: $LIVE/$base" >&2; exit 1; fi
  mv "$item" "$LIVE/$base"
done
sync
printf '%s\n' "Redis legacy layout migration completed: $LIVE"
