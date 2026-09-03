#!/usr/bin/env bash
set -euo pipefail

WINDOW_MINUTES="${AITOS_DIAGNOSTIC_LOG_MINUTES:-30}"
REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
PAPER_CONTAINER="${AITOS_PAPER_CONTAINER:-aitos-paper}"
FAILURES=0

printf '%s\n' '--- Production market-data forensic audit ---'
printf 'Window: last %s minutes\n' "$WINDOW_MINUTES"

if ! docker inspect "$REDIS_CONTAINER" >/dev/null 2>&1; then
  echo 'BLOCKER: Redis container unavailable for forensic audit.'
  exit 1
fi

redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

printf '%s\n' '--- Redis pressure attribution snapshot ---'
if [ -f scripts/redis_forensic_snapshot.sh ]; then
  AITOS_DIAGNOSTIC_LOG_MINUTES="$WINDOW_MINUTES" \
    AITOS_REDIS_CONTAINER="$REDIS_CONTAINER" \
    bash scripts/redis_forensic_snapshot.sh || true
else
  echo 'WARNING: Redis pressure snapshot helper is missing.'
fi

printf '%s\n' '--- Canonical semantic streams ---'
for key in stream:market.trade stream:market.book.snapshot stream:market.book.delta stream:market.ticker stream:market.kline stream:market.funding stream:market.open_interest stream:market.liquidation stream:market.options stream:market.instrument; do
  if redis EXISTS "$key" 2>/dev/null | grep -q '^1$'; then
    echo "STREAM $key"
    echo "  XLEN=$(redis XLEN "$key" 2>/dev/null || echo 0)"
    redis --json XINFO GROUPS "$key" 2>/dev/null || redis XINFO GROUPS "$key" 2>/dev/null || true
  fi
done

printf '%s\n' '--- Canonical scanner PEL summary ---'
SCANNER_GROUP="${AITOS_SCANNER_GROUP:-market-scanner}"
for key in stream:market.trade stream:market.book.snapshot; do
  if redis EXISTS "$key" 2>/dev/null | grep -q '^1$' && redis XINFO GROUPS "$key" 2>/dev/null | grep -q "$SCANNER_GROUP"; then
    pending="$(redis XPENDING "$key" "$SCANNER_GROUP" 2>/dev/null | awk 'NR==1 {print $1}' || echo 0)"
    echo "  $key group=$SCANNER_GROUP pending=$pending"
    redis --json XPENDING "$key" "$SCANNER_GROUP" - + 20 2>/dev/null || true
    if [ "${pending:-0}" -gt 0 ]; then
      echo "BLOCKER: $SCANNER_GROUP has $pending pending entries on $key"
      FAILURES=$((FAILURES + 1))
    fi
  fi
done

printf '%s\n' '--- DLQ forensic summary ---'
if redis EXISTS stream:dlq 2>/dev/null | grep -q '^1$'; then
  dlq_len="$(redis XLEN stream:dlq 2>/dev/null || echo 0)"
  echo "DLQ XLEN=$dlq_len"
  echo 'Recent DLQ entries:'
  redis --json XREVRANGE stream:dlq + - COUNT 50 2>/dev/null || true
  echo 'DLQ reason/source fields (recent 100 entries):'
  redis --raw XREVRANGE stream:dlq + - COUNT 100 2>/dev/null | tail -n 600 || true
else
  echo 'DLQ stream does not exist.'
fi

printf '%s\n' '--- Live market-data stage telemetry ---'
if docker inspect "$PAPER_CONTAINER" >/dev/null 2>&1; then
  logs="$(docker logs --since "${WINDOW_MINUTES}m" --timestamps "$PAPER_CONTAINER" 2>&1 || true)"

  for marker in 'market-data websocket receive lag' 'trade source/parser attribution' 'depth source/parser attribution' 'live state trade processing latency' 'redis xadd latency' 'event-loop scheduling lag'; do
    echo "Telemetry: $marker"
    printf '%s\n' "$logs" | grep "$marker" | tail -n 100 || true
  done

  echo 'Freshness telemetry:'
  printf '%s\n' "$logs" | grep 'live scanner freshness' | tail -n 200 || true

  handler_failures="$(printf '%s\n' "$logs" | grep -Ec 'handler failed for event|trade downstream processing failed|trade state update failed|REST trade recovery failed' || true)"
  echo "handler/downstream failures in last ${WINDOW_MINUTES}m: $handler_failures"
  if [ "$handler_failures" -gt 0 ]; then
    printf '%s\n' "$logs" | grep -E 'handler failed for event|trade downstream processing failed|trade state update failed|REST trade recovery failed' | tail -n 200 || true
    FAILURES=$((FAILURES + 1))
  fi

  paper_count="$(printf '%s\n' "$logs" | grep -c 'paper signal diagnostics' || true)"
  echo "paper signal diagnostics in last ${WINDOW_MINUTES}m: $paper_count"
  rest_fallback_count="$(printf '%s\n' "$logs" | grep 'paper signal diagnostics' | grep -c 'rest_fallback' || true)"
  websocket_live_count="$(printf '%s\n' "$logs" | grep 'paper signal diagnostics' | grep -c 'websocket_live_state' || true)"
  echo "paper signals using rest_fallback: $rest_fallback_count"
  echo "paper signals using websocket_live_state: $websocket_live_count"
  if [ "$paper_count" -gt 0 ] && [ "$websocket_live_count" -eq 0 ]; then
    echo 'BLOCKER: no websocket_live_state paper signals were observed in the audit window.'
    FAILURES=$((FAILURES + 1))
  fi
else
  echo 'BLOCKER: PAPER container unavailable; live-path validation cannot run.'
  FAILURES=$((FAILURES + 1))
fi

printf '%s\n' '--- Forensic verdict ---'
if [ "$FAILURES" -eq 0 ]; then
  echo 'PASS: no enforced live market-data failures detected.'
else
  echo "FAIL: $FAILURES enforced live market-data checks failed."
fi
exit "$FAILURES"
