#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL="${AITOS_HEALTH_URL:-http://127.0.0.1:8090/health}"
METRICS_URL="${AITOS_METRICS_URL:-http://127.0.0.1:8090/metrics}"
MAX_LOG_MB="${AITOS_MAX_LOG_MB:-512}"

printf '=== AITOS Paper Runtime Audit ===\n'
printf 'UTC: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if command -v docker >/dev/null 2>&1; then
  echo '--- Docker containers ---'
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo
  echo '--- Container resource usage ---'
  docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.RestartCount}}' 2>/dev/null || true
  echo
  echo '--- Unhealthy/stopped containers ---'
  docker ps -a --filter status=exited --format '{{.Names}}\t{{.Status}}' || true
  docker ps -a --filter health=unhealthy --format '{{.Names}}\t{{.Status}}' || true
  echo
  echo '--- Docker disk usage ---'
  docker system df
  echo
  echo '--- Docker container log sizes ---'
  docker ps -aq | while read -r id; do
    [ -n "$id" ] || continue
    name="$(docker inspect --format '{{.Name}}' "$id" | sed 's#^/##')"
    log_path="$(docker inspect --format '{{.LogPath}}' "$id" 2>/dev/null || true)"
    if [ -n "$log_path" ] && [ -f "$log_path" ]; then
      size_mb="$(du -m "$log_path" | awk '{print $1}')"
      printf '%-40s %s MB\n' "$name" "$size_mb"
      if [ "$size_mb" -ge "$MAX_LOG_MB" ]; then
        printf 'WARNING: %s log is >= %s MB\n' "$name" "$MAX_LOG_MB"
      fi
    fi
  done
else
  echo 'Docker: NOT INSTALLED'
fi

echo
echo '--- Host memory ---'
free -h

echo
echo '--- Host disk ---'
df -h /

echo
echo '--- AITOS paper-trading processes ---'
pgrep -af 'run_paper_trading|aitos' || true

echo
echo '--- AITOS health endpoint ---'
if command -v curl >/dev/null 2>&1; then
  if curl --fail --silent --show-error --max-time 5 "$HEALTH_URL"; then
    echo
    echo 'Health endpoint: PASS'
  else
    echo 'Health endpoint: FAIL/unreachable'
  fi
  echo
echo '--- AITOS metrics endpoint ---'
  if curl --fail --silent --show-error --max-time 5 "$METRICS_URL" | head -n 20; then
    echo 'Metrics endpoint: PASS'
  else
    echo 'Metrics endpoint: FAIL/unreachable'
  fi
else
  echo 'curl: NOT INSTALLED; skipped health/metrics checks'
fi

echo
echo 'Audit complete. Treat unhealthy containers, repeated restarts, failed health checks, excessive logs, high memory use, or low disk space as blockers before live trading.'
