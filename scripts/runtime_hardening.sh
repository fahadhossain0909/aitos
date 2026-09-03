#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/aitos}"
DATA_ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}"
REDIS_MAXMEMORY_BYTES="${REDIS_MAXMEMORY_BYTES:-2147483648}"
REDIS_CPUS="${REDIS_CPUS:-0.75}"
PAPER_CPUS="${PAPER_CPUS:-0.75}"
LOCK_FILE="${HOME}/.aitos-deploy.lock"

cd "$APP_DIR"
exec 9>"$LOCK_FILE"
flock 9

# The compose file is the source of truth. Reconcile the running Redis
# instance as well so a previously-created container cannot retain an old
# unlimited maxmemory setting across deployments.
docker compose config >/dev/null
docker compose up -d redis

for attempt in 1 2 3 4 5; do
  if docker inspect aitos-redis >/dev/null 2>&1 && \
     [ "$(docker inspect --format '{{.State.Running}}' aitos-redis 2>/dev/null || echo false)" = "true" ] && \
     [ "$(docker exec aitos-redis redis-cli PING 2>/dev/null || true)" = "PONG" ]; then
    break
  fi
  sleep 3
done

# Run-time CPU ceilings are reconciled explicitly because existing containers
# may have been created from an older compose revision. Docker's --cpus value
# is a CFS quota; the previous 0.5-core ceilings were producing throttling
# during market-data bursts even though Redis command execution was fast.
docker update --cpus "$REDIS_CPUS" aitos-redis >/dev/null
echo "Redis CPU ceiling reconciled: $REDIS_CPUS cores"

redis_maxmemory="$(docker exec aitos-redis redis-cli CONFIG GET maxmemory 2>/dev/null | tail -n 1 | tr -d '\r')"
redis_policy="$(docker exec aitos-redis redis-cli CONFIG GET maxmemory-policy 2>/dev/null | tail -n 1 | tr -d '\r')"

if [ "$redis_maxmemory" != "$REDIS_MAXMEMORY_BYTES" ] || [ "$redis_policy" != "noeviction" ]; then
  echo "Reconciling Redis runtime policy: maxmemory=$REDIS_MAXMEMORY_BYTES policy=noeviction"
  docker exec aitos-redis redis-cli CONFIG SET maxmemory "$REDIS_MAXMEMORY_BYTES"
  docker exec aitos-redis redis-cli CONFIG SET maxmemory-policy noeviction
fi

redis_maxmemory="$(docker exec aitos-redis redis-cli CONFIG GET maxmemory 2>/dev/null | tail -n 1 | tr -d '\r')"
redis_policy="$(docker exec aitos-redis redis-cli CONFIG GET maxmemory-policy 2>/dev/null | tail -n 1 | tr -d '\r')"
if [ "$redis_maxmemory" != "$REDIS_MAXMEMORY_BYTES" ] || [ "$redis_policy" != "noeviction" ]; then
  echo "ERROR: Redis runtime hardening verification failed: maxmemory=$redis_maxmemory policy=$redis_policy" >&2
  exit 1
fi

echo "Redis runtime hardened: maxmemory=$redis_maxmemory policy=$redis_policy"
echo "Redis memory:"
docker exec aitos-redis redis-cli INFO memory | grep -E '^(used_memory:|used_memory_human:|maxmemory:|maxmemory_human:|mem_fragmentation_ratio:)' || true
echo "Redis stats:"
docker exec aitos-redis redis-cli INFO stats | grep -E '^(evicted_keys:|expired_keys:|instantaneous_ops_per_sec:)' || true

# Live market-state and trade-price groups must not replay an obsolete
# processing epoch after deployment. Reset only these groups to the current
# tail. Durable history consumers are deliberately untouched.
reset_live_group() {
  local pattern="$1"
  local group="$2"
  local stream
  while IFS= read -r stream; do
    [ -z "$stream" ] && continue
    if docker exec aitos-redis redis-cli XINFO GROUPS "$stream" >/dev/null 2>&1; then
      docker exec aitos-redis redis-cli XGROUP SETID "$stream" "$group" '$' >/dev/null 2>&1 || true
      echo "Live cursor reset: stream=$stream group=$group"
    fi
  done < <(docker exec aitos-redis redis-cli --scan --pattern "$pattern" 2>/dev/null || true)
}

reset_live_group 'stream:market.live_state.*' 'market-os-live-state'
reset_live_group 'stream:market.trade.*' 'trade-lifecycle-prices'
reset_live_group 'stream:market.kline.*' 'trade-lifecycle-prices'

# Restart only the paper-trading application after Redis reconciliation and
# live-cursor reset. This guarantees its new consumers begin from the current
# market epoch rather than replaying stale prices/state.
docker compose up -d --force-recreate aitos-paper

docker update --cpus "$PAPER_CPUS" aitos-paper >/dev/null
echo "Paper CPU ceiling reconciled: $PAPER_CPUS cores"

if ! docker inspect aitos-paper >/dev/null 2>&1 || [ "$(docker inspect --format '{{.State.Running}}' aitos-paper 2>/dev/null || echo false)" != "true" ]; then
  echo "ERROR: aitos-paper failed to remain running after hardening." >&2
  docker logs --tail 100 aitos-paper 2>&1 || true
  exit 1
fi

if mountpoint -q "$DATA_ROOT" 2>/dev/null; then
  df -h "$DATA_ROOT"
fi

echo "Production runtime hardening completed successfully."
