#!/bin/sh
set -eu

REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
ARCHIVE_ROOT="${REDIS_ARCHIVE_ROOT:-/redis-data/archive}"
INTERVAL="${REDIS_ARCHIVE_INTERVAL_SECONDS:-30}"
BATCH_SIZE="${REDIS_ARCHIVE_BATCH_SIZE:-1000}"

mkdir -p "$ARCHIVE_ROOT"

# Archive only streams that are explicitly configured by the producer.
# The archive is append-only on the data disk; Redis is never trimmed by
# this worker. Hot-stream retention remains the producer's responsibility.
STREAMS="${REDIS_ARCHIVE_STREAMS:-}"

while :; do
  if [ -n "$STREAMS" ]; then
    OLDIFS="$IFS"
    IFS=,
    set -- $STREAMS
    IFS="$OLDIFS"
    for stream in "$@"; do
      [ -n "$stream" ] || continue
      safe_name=$(printf '%s' "$stream" | tr '/:' '__')
      file="$ARCHIVE_ROOT/$safe_name.jsonl"
      # XRANGE is intentionally bounded per cycle. A future production
      # implementation can persist per-stream IDs for exact once-only replay.
      redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" --raw XREVRANGE "$stream" + - COUNT "$BATCH_SIZE" 2>/dev/null |
        awk 'NR % 2 == 1 {id=$0; next} {printf "{\"id\":\"%s\",\"data\":%s}\n", id, $0}' >> "$file" || true
    done
  fi
  sleep "$INTERVAL"
done
