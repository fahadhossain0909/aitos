#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${AITOS_CONSUMER_FORENSICS_DIR:-$HOME/aitos-consumer-identity-forensics}"
DURATION="${AITOS_CONSUMER_FORENSICS_DURATION_SECONDS:-180}"
INTERVAL="${AITOS_CONSUMER_FORENSICS_INTERVAL_SECONDS:-10}"
mkdir -p "$OUT_DIR/snapshots"

REDIS_CONTAINER="${REDIS_CONTAINER:-}"
if [[ -z "$REDIS_CONTAINER" ]]; then
  REDIS_CONTAINER="$(docker ps --format '{{.Names}}' | grep -E '(^|[-_])redis($|[-_])' | head -1 || true)"
fi
if [[ -z "$REDIS_CONTAINER" ]]; then
  echo "Redis container not found" >&2
  exit 1
fi

streams=(
  market.trade market.book.delta market.book.snapshot market.ticker
  market.funding market.open_interest market.liquidation market.options
  market.instrument
)

redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

start_epoch="$(date +%s)"
index=0
while :; do
  now="$(date -Is)"
  epoch="$(date +%s)"
  snapshot="$OUT_DIR/snapshots/$(printf '%05d' "$index")"
  mkdir -p "$snapshot"
  printf '%s\n' "$now" > "$snapshot/timestamp.txt"

  docker inspect -f '{{.RestartCount}} {{.State.StartedAt}}' "$REDIS_CONTAINER" \
    > "$snapshot/redis_runtime.txt" 2>&1 || true

  for stream in "${streams[@]}"; do
    key="stream:$stream"
    if redis EXISTS "$key" | grep -q '^1$'; then
      redis --json XINFO CONSUMERS "$key" persistence \
        > "$snapshot/${stream}.json" 2>&1 || true
    else
      printf '[]\n' > "$snapshot/${stream}.json"
    fi
  done

  index=$((index + 1))
  if (( epoch - start_epoch >= DURATION )); then
    break
  fi
  sleep "$INTERVAL"
done

python3 "$(dirname "$0")/analyze_consumer_identity_forensics.py" "$OUT_DIR"
