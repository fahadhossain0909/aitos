#!/usr/bin/env bash
set -euo pipefail

# Read-only diagnostic for the live trade path.
# It correlates the newest Redis trade-stream entries, consumer-group cursors,
# and recent paper-container logs without changing runtime state.

REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
PAPER_CONTAINER="${AITOS_PAPER_CONTAINER:-aitos-paper}"
SYMBOLS=(BTCUSDT ETHUSDT SOLUSDT BNBUSDT)
TRADE_GROUP="${AITOS_TRADE_GROUP:-live-scanner-trades-v2}"
WINDOW="${AITOS_TRACE_LOG_WINDOW:-10m}"

rcli() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

printf '%s\n' '=== AITOS trade-path diagnostic (READ ONLY) ==='
printf 'UTC: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'Redis: %s | paper: %s | log window: %s\n\n' "$REDIS_CONTAINER" "$PAPER_CONTAINER" "$WINDOW"

for symbol in "${SYMBOLS[@]}"; do
  key="stream:market.trade.${symbol}"
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
  echo 'GROUP_CONSUMERS:'
  rcli XINFO CONSUMERS "$key" "$TRADE_GROUP" 2>/dev/null || true
  echo
 done

printf '%s\n' '--- Paper logs: trade path markers ---'
docker logs --since "$WINDOW" --timestamps "$PAPER_CONTAINER" 2>&1 \
  | grep -E 'published event|trade downstream processing failed|trade state update failed|ignored stale trade in live scanner|live scanner freshness|REST trade recovery failed|aggregate-trade stream recovered|aggregate-trade stream idle|raw-trade fallback event invalid' \
  | tail -n 400 || true

printf '%s\n' '--- Per-symbol live scanner freshness markers ---'
docker logs --since "$WINDOW" --timestamps "$PAPER_CONTAINER" 2>&1 \
  | grep 'live scanner freshness' \
  | tail -n 100 || true

printf '%s\n' '--- Interpretation ---'
echo '1. If Redis LATEST_ENTRY timestamp/trade_id is current but live scanner freshness is old: inspect consumer delivery/group ownership.'
echo '2. If Redis LATEST_ENTRY timestamp/trade_id is old while ingestion says WebSocket messages are current: inspect producer/publish path.'
echo '3. If live scanner logs "ignored stale trade" with current delivery time: producer is delivering stale payload timestamps; inspect Binance parser/stream source.'
echo '4. If XINFO CONSUMERS shows another consumer in the same live-scanner group: that consumer can steal messages from the scanner.'
echo '5. Compare Redis stream entry IDs with group last-delivered-id to locate the exact Redis boundary.'
