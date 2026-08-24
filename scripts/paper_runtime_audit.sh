#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL="${AITOS_HEALTH_URL:-http://127.0.0.1:8090/health}"
METRICS_URL="${AITOS_METRICS_URL:-http://127.0.0.1:8090/metrics}"
MAX_LOG_MB="${AITOS_MAX_LOG_MB:-512}"
MIN_DISK_FREE_GB="${AITOS_MIN_DISK_FREE_GB:-10}"

blockers=0

printf '=== AITOS Paper Runtime Audit ===\n'
printf 'UTC: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if command -v docker >/dev/null 2>&1; then
  echo '--- Docker containers ---'
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo

  echo '--- Container resource usage ---'
  docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null || true
  echo

  echo '--- Unhealthy/stopped AITOS containers ---'
  unhealthy="$(docker ps -a --filter health=unhealthy --format '{{.Names}}' | grep '^aitos-' || true)"
  stopped="$(docker ps -a --filter status=exited --format '{{.Names}}' | grep '^aitos-' || true)"
  if [ -n "$unhealthy" ]; then
    printf '%s\n' "$unhealthy"
    blockers=1
  else
    echo 'none'
  fi
  if [ -n "$stopped" ]; then
    printf '%s\n' "$stopped"
    blockers=1
  else
    echo 'none'
  fi
  echo

  echo '--- Container restart counts ---'
  while read -r container; do
    [ -n "$container" ] || continue
    restart_count="$(docker inspect --format '{{.RestartCount}}' "$container" 2>/dev/null || echo 0)"
    status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)"
    printf '%-40s restart=%s status=%s\n' "$container" "$restart_count" "$status"
    if [ "$restart_count" -gt 0 ]; then
      blockers=1
    fi
  done < <(docker ps -a --format '{{.Names}}' | grep '^aitos-' || true)
  echo

  echo '--- Docker disk usage ---'
  docker system df
  echo

  echo '--- Docker container log sizes ---'
  while read -r id; do
    [ -n "$id" ] || continue
    name="$(docker inspect --format '{{.Name}}' "$id" | sed 's#^/##')"
    case "$name" in
      aitos-*) ;;
      *) continue ;;
    esac
    log_path="$(docker inspect --format '{{.LogPath}}' "$id" 2>/dev/null || true)"
    if [ -n "$log_path" ] && [ -f "$log_path" ]; then
      size_mb="$(du -m "$log_path" | awk '{print $1}')"
      printf '%-40s %s MB\n' "$name" "$size_mb"
      if [ "$size_mb" -ge "$MAX_LOG_MB" ]; then
        printf 'BLOCKER: %s log is >= %s MB\n' "$name" "$MAX_LOG_MB"
        blockers=1
      fi
    fi
  done < <(docker ps -aq)
else
  echo 'Docker: NOT INSTALLED'
  blockers=1
fi

echo
echo '--- Host memory ---'
free -h

echo
echo '--- Host disk ---'
df -h /
avail_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
min_kb="$((MIN_DISK_FREE_GB * 1024 * 1024))"
if [ "$avail_kb" -lt "$min_kb" ]; then
  echo "BLOCKER: less than ${MIN_DISK_FREE_GB}GB free on /"
  blockers=1
fi

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
    blockers=1
  fi
  echo
echo '--- AITOS metrics endpoint ---'
  if curl --fail --silent --show-error --max-time 5 "$METRICS_URL" | head -n 20; then
    echo 'Metrics endpoint: PASS'
  else
    echo 'Metrics endpoint: FAIL/unreachable'
    blockers=1
  fi
else
  echo 'curl: NOT INSTALLED; skipped health/metrics checks'
  blockers=1
fi

echo
echo '=== Audit result ==='
if [ "$blockers" -eq 0 ]; then
  echo 'PASS: no production runtime blockers detected.'
else
  echo "FAIL: $blockers production runtime blocker(s) detected."
  exit 1
fi
