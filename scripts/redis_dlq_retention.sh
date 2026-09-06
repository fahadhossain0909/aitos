#!/usr/bin/env sh
set -eu

REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASSWORD="${REDIS_PASSWORD:?REDIS_PASSWORD is required}"
DLQ_MAXLEN="${REDIS_DLQ_MAXLEN:-25000}"
INTERVAL_SECONDS="${REDIS_DLQ_TRIM_INTERVAL_SECONDS:-5}"

case "$DLQ_MAXLEN" in
  ''|*[!0-9]*) echo "Invalid REDIS_DLQ_MAXLEN=$DLQ_MAXLEN" >&2; exit 1 ;;
esac
[ "$DLQ_MAXLEN" -gt 0 ] || { echo "REDIS_DLQ_MAXLEN must be > 0" >&2; exit 1; }

while :; do
  if redis-cli --no-auth-warning -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; then
    # Approximate trimming keeps the DLQ bounded without adding an XTRIM
    # operation to every producer path. This is storage-only maintenance.
    redis-cli --no-auth-warning -h "$REDIS_HOST" -p "$REDIS_PORT" -a "$REDIS_PASSWORD" XTRIM stream:dlq MAXLEN '~' "$DLQ_MAXLEN" >/dev/null 2>&1 || true
  fi
  sleep "$INTERVAL_SECONDS"
done
