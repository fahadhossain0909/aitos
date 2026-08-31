#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/aitos}"
DATA_ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}"
REDIS_MAXMEMORY_BYTES="${REDIS_MAXMEMORY_BYTES:-2147483648}"
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

# Restart only the paper-trading application after Redis reconciliation so its
# Redis connection/state clients are guaranteed to use the hardened instance.
docker compose up -d --force-recreate aitos-paper

if ! docker inspect aitos-paper >/dev/null 2>&1 || [ "$(docker inspect --format '{{.State.Running}}' aitos-paper 2>/dev/null || echo false)" != "true" ]; then
  echo "ERROR: aitos-paper failed to remain running after hardening." >&2
  docker logs --tail 100 aitos-paper 2>&1 || true
  exit 1
fi

if mountpoint -q "$DATA_ROOT" 2>/dev/null; then
  df -h "$DATA_ROOT"
fi

echo "Production runtime hardening completed successfully."
