#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL="${AITOS_HEALTH_URL:-http://127.0.0.1:8090/health}"
METRICS_URL="${AITOS_METRICS_URL:-http://127.0.0.1:8090/metrics}"
MAX_LOG_MB="${AITOS_MAX_LOG_MB:-512}"
MIN_DISK_FREE_GB="${AITOS_MIN_DISK_FREE_GB:-10}"
DATA_ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}"

REQUIRED_CONTAINERS=(
  aitos-redis
  aitos-clickhouse
  aitos-neo4j
  aitos-paper
  aitos-learning
  aitos-storage-maintenance
)

ALLOWED_EXITED_PATTERNS=(
  clickhouse-init
  aitos-backtest
  aitos-live
)

blockers=0

is_allowed_exited() {
  local name="$1"
  local pattern
  for pattern in "${ALLOWED_EXITED_PATTERNS[@]}"; do
    case "$name" in
      *"$pattern"*) return 0 ;;
    esac
  done
  return 1
}

printf '=== AITOS Paper Runtime Audit ===\n'
printf 'UTC: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if command -v docker >/dev/null 2>&1; then
  echo '--- Docker containers ---'
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo

  echo '--- Container resource usage ---'
  docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null || true
  echo

  echo '--- Unhealthy AITOS containers ---'
  unhealthy="$(docker ps -a --filter health=unhealthy --format '{{.Names}}' | grep '^aitos-' || true)"
  if [ -n "$unhealthy" ]; then
    printf '%s\n' "$unhealthy"
    blockers=$((blockers + 1))
    while read -r container; do
      [ -n "$container" ] || continue
      echo "--- Healthcheck diagnostics: $container ---"
      docker inspect --format 'Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} ExitCode={{.State.ExitCode}} Error={{.State.Error}}' "$container" || true
      docker inspect --format '{{range .State.Health.Log}}time={{.Start}} exit={{.ExitCode}} output={{printf "%q" .Output}}\n{{end}}' "$container" 2>/dev/null | tail -n 10 || true
      echo "--- Recent logs: $container ---"
      docker logs --tail 100 --timestamps "$container" 2>&1 || true
      echo
    done <<< "$unhealthy"
  else
    echo 'none'
  fi
  echo

  echo '--- Required runtime containers ---'
  for container in "${REQUIRED_CONTAINERS[@]}"; do
    if ! docker inspect "$container" >/dev/null 2>&1; then
      echo "BLOCKER: required container missing: $container"
      blockers=$((blockers + 1))
      continue
    fi
    status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo none)"
    exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null || echo '?')"
    printf '%-40s status=%s health=%s exit=%s\n' "$container" "$status" "$health" "$exit_code"
    if [ "$status" != "running" ]; then
      echo "BLOCKER: required container not running: $container"
      blockers=$((blockers + 1))
      echo "--- Diagnostics: $container ---"
      docker inspect --format 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} Error={{.State.Error}} OOM={{.State.OOMKilled}}' "$container" || true
      docker logs --tail 100 --timestamps "$container" 2>&1 || true
      echo
    fi
  done
  echo

  echo '--- Other exited AITOS containers (informational) ---'
  other_stopped=0
  while read -r container; do
    [ -n "$container" ] || continue
    skip=0
    for req in "${REQUIRED_CONTAINERS[@]}"; do
      if [ "$container" = "$req" ]; then skip=1; break; fi
    done
    [ "$skip" -eq 1 ] && continue
    if is_allowed_exited "$container"; then
      exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null || echo '?')"
      printf 'allowed one-shot: %-40s exit=%s\n' "$container" "$exit_code"
      continue
    fi
    other_stopped=1
    exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null || echo '?')"
    printf 'unexpected exited: %-40s exit=%s\n' "$container" "$exit_code"
    blockers=$((blockers + 1))
    echo "--- Diagnostics: $container ---"
    docker logs --tail 50 --timestamps "$container" 2>&1 || true
  done < <(docker ps -a --filter status=exited --format '{{.Names}}' | grep '^aitos-' || true)
  if [ "$other_stopped" -eq 0 ]; then
    echo 'none unexpected'
  fi
  echo

  echo '--- Container restart counts ---'
  while read -r container; do
    [ -n "$container" ] || continue
    restart_count="$(docker inspect --format '{{.RestartCount}}' "$container" 2>/dev/null || echo 0)"
    status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)"
    printf '%-40s restart=%s status=%s\n' "$container" "$restart_count" "$status"
    if [ "$restart_count" -gt 0 ]; then
      echo "WARNING: $container has historical Docker restarts; current runtime state is evaluated separately."
    fi
  done < <(docker ps -a --format '{{.Names}}' | grep '^aitos-' || true)
  echo

  echo '--- Database storage mounts ---'
  for container in aitos-clickhouse aitos-neo4j aitos-redis; do
    if docker inspect "$container" >/dev/null 2>&1; then
      printf '%s\n' "$container"
      docker inspect --format '{{range .Mounts}}{{printf "  %s -> %s (%s)\n" .Source .Destination .Type}}{{end}}' "$container" || true
    fi
  done
  if command -v findmnt >/dev/null 2>&1; then
    echo '--- Data disk mount ---'
    findmnt -T "$DATA_ROOT" -o SOURCE,FSTYPE,SIZE,USED,AVAIL,USE%,TARGET 2>/dev/null || echo "WARNING: $DATA_ROOT is not mounted"
  fi
  echo

  echo '--- ClickHouse storage/merge diagnostics ---'
  if docker inspect aitos-clickhouse >/dev/null 2>&1; then
    docker exec aitos-clickhouse clickhouse-client --query \
      "SELECT table, formatReadableSize(sum(bytes_on_disk)) AS disk, sum(rows) AS rows, count() AS active_parts FROM system.parts WHERE database='aitos' AND active GROUP BY table ORDER BY sum(bytes_on_disk) DESC FORMAT PrettyCompactMonoBlock" 2>/dev/null || echo 'ClickHouse table inventory unavailable'
    echo
    docker exec aitos-clickhouse clickhouse-client --query \
      "SELECT count() AS active_merges, formatReadableSize(sum(bytes_read_uncompressed)) AS bytes_read, formatReadableSize(sum(bytes_written_uncompressed)) AS bytes_written FROM system.merges FORMAT PrettyCompactMonoBlock" 2>/dev/null || echo 'ClickHouse merge inventory unavailable'
    echo
    docker exec aitos-clickhouse clickhouse-client --query \
      "SELECT count() AS pending_mutations FROM system.mutations WHERE database='aitos' AND is_done=0 FORMAT PrettyCompactMonoBlock" 2>/dev/null || echo 'ClickHouse mutation inventory unavailable'
  fi
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
        blockers=$((blockers + 1))
      fi
    fi
  done < <(docker ps -aq)

  echo
  echo '--- Trade downstream exception diagnostics ---'
  echo 'The following lines are observational only; they do not change audit blocker status.'
  echo 'Looking for sink failures from the live paper-trading process:'
  docker logs --since 30m --timestamps aitos-paper 2>&1 \
    | grep -E 'trade downstream processing failed|trade state update failed|REST trade recovery failed' \
    | tail -n 100 || true
  echo

  echo '--- Trade downstream exception summary ---'
  downstream_count="$(docker logs --since 30m aitos-paper 2>&1 | grep -c 'trade downstream processing failed' || true)"
  echo "trade downstream exceptions in last 30m: $downstream_count"
else
  echo 'Docker: NOT INSTALLED'
  blockers=$((blockers + 1))
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
  blockers=$((blockers + 1))
fi

echo
echo '--- AITOS paper-trading processes ---'
pgrep -af 'run_paper_trading|aitos' || true

echo
echo '--- AITOS health endpoint ---'
if command -v curl >/dev/null 2>&1; then
  health_body="$(mktemp)"
  health_code="000"
  if health_code="$(curl --silent --show-error --max-time 5 -o "$health_body" -w '%{http_code}' "$HEALTH_URL" 2>/dev/null)"; then
    :
  else
    health_code="000"
  fi
  echo "Health HTTP status: $health_code"
  echo 'Health response body:'
  cat "$health_body" || true
  echo
  if [ "$health_code" = "200" ]; then
    echo 'Health endpoint: PASS'
  else
    echo 'Health endpoint: FAIL/unreachable'
    blockers=$((blockers + 1))
    if docker inspect aitos-paper >/dev/null 2>&1; then
      echo '--- aitos-paper diagnostics after health failure ---'
      docker inspect --format 'State={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} ExitCode={{.State.ExitCode}} Error={{.State.Error}}' aitos-paper || true
      docker inspect --format '{{range .State.Health.Log}}time={{.Start}} exit={{.ExitCode}} output={{printf "%q" .Output}}\n{{end}}' aitos-paper 2>/dev/null | tail -n 10 || true
      echo '--- Last 150 aitos-paper log lines ---'
      docker logs --tail 150 --timestamps aitos-paper 2>&1 || true
    fi
  fi
  rm -f "$health_body"
  echo
  echo '--- AITOS metrics endpoint ---'
  metrics_body="$(mktemp)"
  metrics_code="000"
  if metrics_code="$(curl --silent --show-error --max-time 5 -o "$metrics_body" -w '%{http_code}' "$METRICS_URL" 2>/dev/null)"; then
    :
  else
    metrics_code="000"
  fi
  echo "Metrics HTTP status: $metrics_code"
  head -n 40 "$metrics_body" || true
  echo
  if [ "$metrics_code" = "200" ]; then
    echo 'Metrics endpoint: PASS'
  else
    echo 'Metrics endpoint: FAIL/unreachable'
    blockers=$((blockers + 1))
  fi
  rm -f "$metrics_body"
else
  echo 'curl: NOT INSTALLED; skipped health/metrics checks'
  blockers=$((blockers + 1))
fi

echo
echo '=== Audit result ==='
if [ "$blockers" -eq 0 ]; then
  echo 'PASS: no production runtime blockers detected.'
else
  echo "FAIL: $blockers production runtime blocker(s) detected."
  exit 1
fi
