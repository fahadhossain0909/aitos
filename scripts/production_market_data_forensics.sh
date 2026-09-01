#!/usr/bin/env bash
set -euo pipefail

WINDOW_MINUTES="${AITOS_DIAGNOSTIC_LOG_MINUTES:-30}"
REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
PAPER_CONTAINER="${AITOS_PAPER_CONTAINER:-aitos-paper}"

printf '%s\n' '--- Production market-data forensic audit ---'
printf 'Window: last %s minutes\n' "$WINDOW_MINUTES"

if ! docker inspect "$REDIS_CONTAINER" >/dev/null 2>&1; then
  echo 'BLOCKER: Redis container unavailable for forensic audit.'
  exit 1
fi

redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

printf '%s\n' '--- Critical consumer groups ---'
for pattern in 'stream:market.trade.*' 'stream:market.orderbook.*' 'stream:market.liquidity.*' 'stream:market.live_state.*'; do
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    echo "STREAM $key"
    redis --json XINFO GROUPS "$key" 2>/dev/null || redis XINFO GROUPS "$key" 2>/dev/null || true
  done < <(redis --raw --scan --pattern "$pattern" 2>/dev/null || true)
done

printf '%s\n' '--- LiveScanner PEL summary ---'
for group in live-scanner-trades-v2 live-scanner-book-v2 live-scanner-liquidity-v2; do
  echo "GROUP $group"
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    if redis XINFO GROUPS "$key" 2>/dev/null | grep -q "$group"; then
      pending="$(redis XPENDING "$key" "$group" 2>/dev/null | awk 'NR==1 {print $1}' || echo 0)"
      echo "  $key pending=$pending"
      redis --json XPENDING "$key" "$group" - + 20 2>/dev/null || true
    fi
  done < <(redis --raw --scan --pattern 'stream:market.*' 2>/dev/null || true)
done

printf '%s\n' '--- DLQ forensic summary ---'
if redis EXISTS stream:dlq 2>/dev/null | grep -q '^1$'; then
  dlq_len="$(redis XLEN stream:dlq 2>/dev/null || echo 0)"
  echo "DLQ XLEN=$dlq_len"
  echo 'Recent DLQ entries:'
  redis --json XREVRANGE stream:dlq + - COUNT 50 2>/dev/null || true
  echo 'DLQ reason fingerprints (recent 500):'
  redis --raw XREVRANGE stream:dlq + - COUNT 500 2>/dev/null | \
    awk 'NR%2==0' | grep '^dlq_reason$' -A1 | tail -n +2 | \
    sort | uniq -c | sort -nr | head -n 20 || true
  echo 'DLQ original-stream fingerprints (recent 500):'
  redis --raw XREVRANGE stream:dlq + - COUNT 500 2>/dev/null | \
    awk 'NR%2==0' | grep '^original_stream$' -A1 | tail -n +2 | \
    sort | uniq -c | sort -nr | head -n 20 || true
else
  echo 'DLQ stream does not exist.'
fi

printf '%s\n' '--- LiveScanner log telemetry ---'
if docker inspect "$PAPER_CONTAINER" >/dev/null 2>&1; then
  logs="$(docker logs --since "${WINDOW_MINUTES}m" --timestamps "$PAPER_CONTAINER" 2>&1 || true)"
  echo 'Freshness summary:'
  printf '%s\n' "$logs" | grep 'live scanner freshness' | tail -n 200 || true
  echo 'Stale/duplicate counters:'
  printf '%s\n' "$logs" | grep -E 'discarded stale live trade|live scanner freshness' | tail -n 200 || true
  echo 'Handler failures:'
  printf '%s\n' "$logs" | grep -E 'handler failed for event|trade downstream processing failed|trade state update failed|REST trade recovery failed' | tail -n 200 || true
  echo 'Paper source distribution:'
  printf '%s\n' "$logs" | grep 'paper signal diagnostics' | \
    sed -n 's/.*market_source[=: ]\+\([^,} ]*\).*/\1/p' | sort | uniq -c | sort -nr || true
else
  echo 'PAPER container unavailable; log telemetry skipped.'
fi

printf '%s\n' '--- Forensic interpretation thresholds ---'
echo 'PASS: critical LiveScanner groups have pending=0 and recent freshness telemetry is present.'
echo 'PASS: paper signals use websocket_live_state when WS data is fresh.'
echo 'PASS: DLQ has no new entries in the audit window (when timestamp-level data is available).'
echo 'BLOCKER candidate: persistent pending entries, handler exceptions, stale cache, or REST fallback for a WS-required strategy.'
