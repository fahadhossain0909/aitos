#!/usr/bin/env bash
set -euo pipefail

# Read-only diagnostic for the canonical live trade path.
# It correlates the newest Redis semantic trade stream, the single scanner
# consumer group, and recent paper-container logs without changing state.

REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
PAPER_CONTAINER="${AITOS_PAPER_CONTAINER:-aitos-paper}"
SYMBOLS=(BTCUSDT ETHUSDT SOLUSDT BNBUSDT)
TRADE_GROUP="${AITOS_TRADE_GROUP:-market-scanner}"
TRADE_STREAM_PREFIX="stream:market.trade"
WINDOW="${AITOS_TRACE_LOG_WINDOW:-10m}"

rcli() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

printf '%s\n' '=== AITOS canonical trade-path diagnostic (READ ONLY) ==='
printf 'UTC: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'Redis: %s | paper: %s | log window: %s\n\n' "$REDIS_CONTAINER" "$PAPER_CONTAINER" "$WINDOW"

for symbol in "${SYMBOLS[@]}"; do
  key="${TRADE_STREAM_PREFIX}.${symbol}"
  printf '%s\n' "--- ${symbol}: Redis stream ${key} ---"
  if ! rcli EXISTS "$key" | grep -qx '1'; then
    echo 'STREAM_MISSING=1'
    echo
    continue
  fi

  echo "XLEN=$(rcli XLEN "$key")"
  echo 'LATEST_ENTRY:'
  rcli --json XREVRANGE "$key" + - COUNT 1 || rcli XREVRANGE "$key" + - COUNT 1
  echo 'CONSUMER_GROUPS:'
  rcli XINFO GROUPS "$key" 2>/dev/null || true
  echo "SCANNER_GROUP=${TRADE_GROUP}"
  rcli XINFO CONSUMERS "$key" "$TRADE_GROUP" 2>/dev/null || true
  echo
 done

printf '%s\n' '--- Paper logs: canonical trade path markers ---'
docker logs --since "$WINDOW" --timestamps "$PAPER_CONTAINER" 2>&1 \
  | grep -E 'published event|trade downstream processing failed|trade state update failed|canonical scanner subscriptions initialized|market-data websocket receive lag|live scanner freshness|REST trade recovery failed' \
  | tail -n 400 || true

printf '%s\n' '--- Per-symbol live scanner freshness markers ---'
docker logs --since "$WINDOW" --timestamps "$PAPER_CONTAINER" 2>&1 \
  | grep 'live scanner freshness' \
  | tail -n 100 || true

printf '%s\n' '--- Interpretation ---'
echo '1. Redis LATEST_ENTRY current + scanner freshness old: inspect market-scanner delivery/consumer ownership.'
echo '2. Redis LATEST_ENTRY old + WebSocket receive current: inspect producer/publish boundary.'
echo '3. Stale canonical events: inspect adapter event timestamps/source attribution.'
echo '4. Any unexpected consumer in market-scanner: investigate message ownership before changing transport.'
echo '5. Compare stream entry IDs with market-scanner last-delivered-id to locate the Redis boundary.'
