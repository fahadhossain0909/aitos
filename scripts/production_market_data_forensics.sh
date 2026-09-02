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

printf '%s\n' '--- Critical consumer groups ---'
for pattern in 'stream:market.trade.*' 'stream:market.orderbook.*' 'stream:market.liquidity.*' 'stream:market.live_state.*'; do
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    echo "STREAM $key"
    redis --json XINFO GROUPS "$key" 2>/dev/null || redis XINFO GROUPS "$key" 2>/dev/null || true
  done < <(redis --raw --scan --pattern "$pattern" 2>/dev/null || true)
done

printf '%s\n' '--- LiveScanner v3 PEL summary ---'
for group in live-scanner-trades-v3 live-scanner-book-v3 live-scanner-liquidity-v3; do
  echo "GROUP $group"
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    if redis XINFO GROUPS "$key" 2>/dev/null | grep -q "$group"; then
      pending="$(redis XPENDING "$key" "$group" 2>/dev/null | awk 'NR==1 {print $1}' || echo 0)"
      echo "  $key pending=$pending"
      redis --json XPENDING "$key" "$group" - + 20 2>/dev/null || true
      if [ "${pending:-0}" -gt 0 ]; then
        echo "BLOCKER: $group has $pending pending entries on $key"
        FAILURES=$((FAILURES + 1))
      fi
    fi
  done < <(redis --raw --scan --pattern 'stream:market.*' 2>/dev/null || true)
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

  echo 'WebSocket receive boundary (all actionable receive-lag samples):'
  printf '%s\n' "$logs" | grep -E 'market-data websocket receive lag|market-data websocket receive' | tail -n 300 || true
  ws_lag_count="$(printf '%s\n' "$logs" | grep -c 'market-data websocket receive lag' || true)"
  echo "websocket receive lag samples: $ws_lag_count"

  echo 'Parser boundary:'
  printf '%s\n' "$logs" | grep -E 'trade source/parser attribution|depth source/parser attribution' | tail -n 300 || true
  parser_lag_count="$(printf '%s\n' "$logs" | grep -Ec 'trade source/parser attribution|depth source/parser attribution' || true)"
  echo "parser attribution samples: $parser_lag_count"

  echo 'Live-state CPU boundary:'
  printf '%s\n' "$logs" | grep 'live state trade processing latency' | tail -n 300 || true
  state_lag_count="$(printf '%s\n' "$logs" | grep -c 'live state trade processing latency' || true)"
  echo "live-state latency samples: $state_lag_count"

  echo 'Redis XADD boundary:'
  printf '%s\n' "$logs" | grep 'redis xadd latency' | tail -n 300 || true
  redis_lag_count="$(printf '%s\n' "$logs" | grep -c 'redis xadd latency' || true)"
  echo "redis XADD latency samples: $redis_lag_count"

  echo 'Event-loop boundary:'
  printf '%s\n' "$logs" | grep 'event-loop scheduling lag' | tail -n 300 || true
  loop_lag_count="$(printf '%s\n' "$logs" | grep -c 'event-loop scheduling lag' || true)"
  echo "event-loop lag samples: $loop_lag_count"

  echo 'Stage-lag correlation window (all warning-level forensic records):'
  printf '%s\n' "$logs" | grep -E 'market-data websocket receive lag|trade source/parser attribution|depth source/parser attribution|live state trade processing latency|redis xadd latency|event-loop scheduling lag' | tail -n 500 || true

  echo 'Freshness telemetry:'
  printf '%s\n' "$logs" | grep 'live scanner freshness' | tail -n 200 || true

  echo 'Stale/duplicate telemetry:'
  printf '%s\n' "$logs" | grep -E 'discarded stale live trade|live scanner freshness' | tail -n 200 || true

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
