#!/usr/bin/env bash
set -euo pipefail

# Read-only diagnostic for the canonical live trade path.
# Canonical MarketDataBus uses one semantic stream per event type; symbols are
# fields in the event envelope, not separate consumer groups.

REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
PAPER_CONTAINER="${AITOS_PAPER_CONTAINER:-aitos-paper}"
TRADE_STREAM="${AITOS_TRADE_STREAM:-stream:market.trade}"
TRADE_GROUP="${AITOS_TRADE_GROUP:-market-scanner}"
WINDOW="${AITOS_TRACE_LOG_WINDOW:-10m}"

rcli() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

printf '%s\n' '=== AITOS canonical trade-path diagnostic (READ ONLY) ==='
printf 'UTC: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'Redis: %s | paper: %s | log window: %s\n\n' "$REDIS_CONTAINER" "$PAPER_CONTAINER" "$WINDOW"

printf '%s\n' "--- Canonical trade stream: ${TRADE_STREAM} ---"
if ! rcli EXISTS "$TRADE_STREAM" | grep -qx '1'; then
  echo 'STREAM_MISSING=1'
else
  echo "XLEN=$(rcli XLEN "$TRADE_STREAM")"
  echo 'LATEST_ENTRY:'
  rcli --json XREVRANGE "$TRADE_STREAM" + - COUNT 1 || rcli XREVRANGE "$TRADE_STREAM" + - COUNT 1
  echo 'CONSUMER_GROUPS:'
  rcli XINFO GROUPS "$TRADE_STREAM" 2>/dev/null || true
  echo "SCANNER_GROUP=${TRADE_GROUP}"
  rcli XINFO CONSUMERS "$TRADE_STREAM" "$TRADE_GROUP" 2>/dev/null || true
fi

echo
printf '%s\n' '--- Paper logs: canonical trade path markers ---'
docker logs --since "$WINDOW" --timestamps "$PAPER_CONTAINER" 2>&1 \
  | grep -E 'published event|trade downstream processing failed|trade state update failed|canonical scanner subscriptions initialized|canonical market-data runtime started|market-data websocket receive lag|live scanner freshness|REST trade recovery failed' \
  | tail -n 400 || true

printf '%s\n' '--- Interpretation ---'
echo '1. Canonical trade transport has one semantic stream: stream:market.trade.'
echo '2. market-scanner is the single scanner consumer group; symbols are filtered in the consumer.'
echo '3. If stream entries are current but scanner freshness is old: inspect group delivery/handler latency.'
echo '4. If stream entries are old while WebSocket receive is current: inspect gateway/publish boundary.'
echo '5. REST recovery must remain explicitly marked as degraded/non-live.'
