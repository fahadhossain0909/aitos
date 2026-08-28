#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL="${AITOS_HEALTH_URL:-http://127.0.0.1:8090/health}"
METRICS_URL="${AITOS_METRICS_URL:-http://127.0.0.1:8090/metrics}"
MAX_LOG_MB="${AITOS_MAX_LOG_MB:-512}"
MIN_DISK_FREE_GB="${AITOS_MIN_DISK_FREE_GB:-10}"
DATA_ROOT="${AITOS_DATA_ROOT:-/mnt/aitos-data}"
DIAGNOSTIC_LOG_MINUTES="${AITOS_DIAGNOSTIC_LOG_MINUTES:-30}"
REDIS_MEMORY_WARN_PCT="${REDIS_MEMORY_WARN_PCT:-85}"
REDIS_MEMORY_BLOCK_PCT="${REDIS_MEMORY_BLOCK_PCT:-95}"

REQUIRED_CONTAINERS=(aitos-redis aitos-clickhouse aitos-neo4j aitos-paper aitos-learning aitos-storage-maintenance)
ALLOWED_EXITED_PATTERNS=(clickhouse-init aitos-backtest aitos-live)
blockers=0

is_allowed_exited() { local name="$1" pattern; for pattern in "${ALLOWED_EXITED_PATTERNS[@]}"; do case "$name" in *"$pattern"*) return 0;; esac; done; return 1; }

printf '=== AITOS Paper Runtime Audit ===\n'
printf 'UTC: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if command -v docker >/dev/null 2>&1; then
  echo '--- Docker containers ---'
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo
  echo '--- Container resource usage ---'
  docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null || true
  echo
  echo '--- Redis memory / retention diagnostics ---'
  if docker inspect aitos-redis >/dev/null 2>&1; then
    redis_info="$(docker exec aitos-redis redis-cli INFO memory 2>/dev/null || true)"
    redis_stats="$(docker exec aitos-redis redis-cli INFO stats 2>/dev/null || true)"
    redis_keyspace="$(docker exec aitos-redis redis-cli INFO keyspace 2>/dev/null || true)"
    redis_config="$(docker exec aitos-redis redis-cli CONFIG GET maxmemory maxmemory-policy appendonly appendfsync 2>/dev/null || true)"
    echo "$redis_info" | grep -E '^(used_memory:|used_memory_human:|used_memory_rss:|used_memory_rss_human:|used_memory_peak:|used_memory_peak_human:|maxmemory:|maxmemory_human:|mem_fragmentation_ratio:)' || true
    echo "$redis_stats" | grep -E '^(instantaneous_ops_per_sec:|total_commands_processed:|evicted_keys:|expired_keys:|keyspace_hits:|keyspace_misses:)' || true
    echo "$redis_config" || true
    echo "$redis_keyspace" | grep '^db[0-9]' || true
    used_pct="$(awk -F: '/^used_memory:/{u=$2} /^maxmemory:/{m=$2} END{if(m>0) printf "%.2f", (u/m)*100; else print "0"}' <<< "$redis_info")"
    echo "Redis used_memory/maxmemory: ${used_pct}%"
    if awk -v p="$used_pct" -v b="$REDIS_MEMORY_BLOCK_PCT" 'BEGIN{exit !(p>=b)}'; then
      echo "BLOCKER: Redis memory usage >= ${REDIS_MEMORY_BLOCK_PCT}% of maxmemory"
      blockers=$((blockers + 1))
    elif awk -v p="$used_pct" -v w="$REDIS_MEMORY_WARN_PCT" 'BEGIN{exit !(p>=w)}'; then
      echo "WARNING: Redis memory usage >= ${REDIS_MEMORY_WARN_PCT}% of maxmemory"
    else
      echo "Redis memory usage below warning threshold (${REDIS_MEMORY_WARN_PCT}%)."
    fi
    echo 'Redis stream length inventory (actual stored entries):'
    for pattern in 'stream:market.orderbook.*' 'stream:market.liquidity.*' 'stream:market.live_state.*' 'stream:dlq'; do
      found=0
      for key in $(docker exec aitos-redis redis-cli --raw --scan --pattern "$pattern" 2>/dev/null || true); do
        found=1
        length="$(docker exec aitos-redis redis-cli XLEN "$key" 2>/dev/null || echo '?')"
        printf '  %-70s XLEN=%s\n' "$key" "$length"
      done
      [ "$found" -eq 1 ] || echo "  pattern=$pattern -> no keys"
    done
    echo 'Redis consumer-group pending inventory:'
    for key in $(docker exec aitos-redis redis-cli --raw --scan --pattern 'stream:*' 2>/dev/null || true); do
      groups="$(docker exec aitos-redis redis-cli XINFO GROUPS "$key" 2>/dev/null || true)"
      if [ -n "$groups" ]; then
        echo "  $key"
        docker exec aitos-redis redis-cli XINFO GROUPS "$key" 2>/dev/null | grep -E 'name|pending|consumers|entries-read|lag' || true
      fi
    done
    echo 'Redis key TTL/large-key sampling:'
    docker exec aitos-redis redis-cli --scan 2>/dev/null | head -n 50 | while read -r key; do
      [ -n "$key" ] || continue
      type="$(docker exec aitos-redis redis-cli TYPE "$key" 2>/dev/null || echo unknown)"
      ttl="$(docker exec aitos-redis redis-cli TTL "$key" 2>/dev/null || echo '?')"
      printf '  type=%-8s ttl=%-8s key=%s\n' "$type" "$ttl" "$key"
    done
    echo 'Redis diagnostic interpretation:'
    echo '  - Streams are bounded by MAXLEN where configured; XLEN shows actual retained entries.'
    echo '  - evicted_keys/expired_keys help distinguish eviction/expiry from simple retention.'
    echo '  - consumer-group pending counts expose unacked entries that can accumulate.'
    echo '  - No destructive FLUSHDB/DEL operation is performed by this audit.'
  else
    echo 'Redis container unavailable; memory diagnostics skipped.'
  fi
  echo

  echo '--- Unhealthy AITOS containers ---'
  unhealthy="$(docker ps -a --filter health=unhealthy --format '{{.Names}}' | grep '^aitos-' || true)"
  if [ -n "$unhealthy" ]; then
    printf '%s\n' "$unhealthy"; blockers=$((blockers + 1))
    while read -r container; do
      [ -n "$container" ] || continue
      echo "--- Healthcheck diagnostics: $container ---"
      docker inspect --format 'Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} ExitCode={{.State.ExitCode}} Error={{.State.Error}}' "$container" || true
      docker inspect --format '{{range .State.Health.Log}}time={{.Start}} exit={{.ExitCode}} output={{printf "%q" .Output}}\n{{end}}' "$container" 2>/dev/null | tail -n 10 || true
      echo "--- Recent logs: $container ---"; docker logs --tail 100 --timestamps "$container" 2>&1 || true; echo
    done <<< "$unhealthy"
  else echo 'none'; fi
  echo

  echo '--- Required runtime containers ---'
  for container in "${REQUIRED_CONTAINERS[@]}"; do
    if ! docker inspect "$container" >/dev/null 2>&1; then echo "BLOCKER: required container missing: $container"; blockers=$((blockers + 1)); continue; fi
    status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo none)"
    exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null || echo '?')"
    printf '%-40s status=%s health=%s exit=%s\n' "$container" "$status" "$health" "$exit_code"
    if [ "$status" != "running" ]; then echo "BLOCKER: required container not running: $container"; blockers=$((blockers + 1)); docker inspect --format 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} Error={{.State.Error}} OOM={{.State.OOMKilled}}' "$container" || true; docker logs --tail 100 --timestamps "$container" 2>&1 || true; fi
  done
  echo

  echo '--- Other exited AITOS containers (informational) ---'
  other_stopped=0
  while read -r container; do
    [ -n "$container" ] || continue
    skip=0; for req in "${REQUIRED_CONTAINERS[@]}"; do [ "$container" = "$req" ] && skip=1 && break; done; [ "$skip" -eq 1 ] && continue
    if is_allowed_exited "$container"; then printf 'allowed one-shot: %-40s exit=%s\n' "$container" "$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null || echo '?')"; continue; fi
    other_stopped=1; printf 'unexpected exited: %-40s exit=%s\n' "$container" "$(docker inspect --format '{{.State.ExitCode}}' "$container" 2>/dev/null || echo '?')"; blockers=$((blockers + 1)); docker logs --tail 50 --timestamps "$container" 2>&1 || true
  done < <(docker ps -a --filter status=exited --format '{{.Names}}' | grep '^aitos-' || true)
  [ "$other_stopped" -eq 0 ] && echo 'none unexpected'; echo

  echo '--- Container restart counts ---'
  while read -r container; do [ -n "$container" ] || continue; restart_count="$(docker inspect --format '{{.RestartCount}}' "$container" 2>/dev/null || echo 0)"; status="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)"; printf '%-40s restart=%s status=%s\n' "$container" "$restart_count" "$status"; [ "$restart_count" -gt 0 ] && echo "WARNING: $container has historical Docker restarts; current runtime state is evaluated separately."; done < <(docker ps -a --format '{{.Names}}' | grep '^aitos-' || true)
  echo

  echo '--- Database storage mounts ---'
  for container in aitos-clickhouse aitos-neo4j aitos-redis; do docker inspect "$container" >/dev/null 2>&1 && { printf '%s\n' "$container"; docker inspect --format '{{range .Mounts}}{{printf "  %s -> %s (%s)\n" .Source .Destination .Type}}{{end}}' "$container" || true; }; done
  command -v findmnt >/dev/null 2>&1 && { echo '--- Data disk mount ---'; findmnt -T "$DATA_ROOT" -o SOURCE,FSTYPE,SIZE,USED,AVAIL,USE%,TARGET 2>/dev/null || echo "WARNING: $DATA_ROOT is not mounted"; }
  echo

  echo '--- ClickHouse storage/merge diagnostics ---'
  if docker inspect aitos-clickhouse >/dev/null 2>&1; then
    docker exec aitos-clickhouse clickhouse-client --query "SELECT table, formatReadableSize(sum(bytes_on_disk)) AS disk, sum(rows) AS rows, count() AS active_parts FROM system.parts WHERE database='aitos' AND active GROUP BY table ORDER BY sum(bytes_on_disk) DESC FORMAT PrettyCompactMonoBlock" 2>/dev/null || echo 'ClickHouse table inventory unavailable'
    docker exec aitos-clickhouse clickhouse-client --query "SELECT count() AS active_merges, formatReadableSize(sum(bytes_read_uncompressed)) AS bytes_read, formatReadableSize(sum(bytes_written_uncompressed)) AS bytes_written FROM system.merges FORMAT PrettyCompactMonoBlock" 2>/dev/null || echo 'ClickHouse merge inventory unavailable'
    docker exec aitos-clickhouse clickhouse-client --query "SELECT count() AS pending_mutations FROM system.mutations WHERE database='aitos' AND is_done=0 FORMAT PrettyCompactMonoBlock" 2>/dev/null || echo 'ClickHouse mutation inventory unavailable'
  fi
  echo
  echo '--- Docker disk usage ---'; docker system df; echo
  echo '--- Docker container log sizes ---'
  while read -r id; do [ -n "$id" ] || continue; name="$(docker inspect --format '{{.Name}}' "$id" | sed 's#^/##')"; case "$name" in aitos-*) ;; *) continue;; esac; log_path="$(docker inspect --format '{{.LogPath}}' "$id" 2>/dev/null || true)"; if [ -n "$log_path" ] && [ -f "$log_path" ]; then size_mb="$(du -m "$log_path" | awk '{print $1}')"; printf '%-40s %s MB\n' "$name" "$size_mb"; [ "$size_mb" -ge "$MAX_LOG_MB" ] && { printf 'BLOCKER: %s log is >= %s MB\n' "$name" "$MAX_LOG_MB"; blockers=$((blockers + 1)); }; fi; done < <(docker ps -aq)
  echo
  echo '--- Paper signal diagnostics (last 30m) ---'; echo "Capture window: ${DIAGNOSTIC_LOG_MINUTES} minutes"; echo 'These are observational diagnostics only; they do not change audit blocker status.'
  diagnostic_logs="$(docker logs --since "${DIAGNOSTIC_LOG_MINUTES}m" --timestamps aitos-paper 2>&1 | grep 'paper signal diagnostics' || true)"; [ -n "$diagnostic_logs" ] && printf '%s\n' "$diagnostic_logs" | tail -n 200 || { echo 'NO paper signal diagnostics found in the requested window.'; echo 'This may mean the scanner did not run, the deployed image predates diagnostic logging, or logs are emitted under a different logger/output configuration.'; }; echo
  echo '--- Paper signal diagnostics summary ---'; diagnostic_count="$(printf '%s\n' "$diagnostic_logs" | grep -c 'paper signal diagnostics' || true)"; echo "paper signal diagnostic entries in last ${DIAGNOSTIC_LOG_MINUTES}m: $diagnostic_count"; [ "$diagnostic_count" -gt 0 ] && echo 'Expected fields to inspect per entry: symbol, market_source, live_fresh, executed_trades, structure, candle_cvd, orderflow_bias, orderflow_delta, orderflow_cvd, orderflow_buy_ratio, orderflow_aggression, orderflow_vwap, liquidity_quality, depth_imbalance, liquidity_wall, sweep_potential, absorption_proxy, liquidity_score, direction, scanner_threshold.'; echo
  echo '--- Trade downstream exception diagnostics ---'; echo 'The following lines are observational only; they do not change audit blocker status.'; echo 'Looking for sink failures from the live paper-trading process:'; docker logs --since 30m --timestamps aitos-paper 2>&1 | grep -E 'trade downstream processing failed|trade state update failed|REST trade recovery failed' | tail -n 100 || true; echo
  echo '--- Trade downstream exception summary ---'; downstream_count="$(docker logs --since 30m aitos-paper 2>&1 | grep -c 'trade downstream processing failed' || true)"; echo "trade downstream exceptions in last 30m: $downstream_count"
else echo 'Docker: NOT INSTALLED'; blockers=$((blockers + 1)); fi

echo; echo '--- Host memory ---'; free -h; echo; echo '--- Host disk ---'; df -h /; avail_kb="$(df -Pk / | awk 'NR==2 {print $4}')"; min_kb="$((MIN_DISK_FREE_GB * 1024 * 1024))"; [ "$avail_kb" -lt "$min_kb" ] && { echo "BLOCKER: less than ${MIN_DISK_FREE_GB}GB free on /"; blockers=$((blockers + 1)); }
echo; echo '--- AITOS paper-trading processes ---'; pgrep -af 'run_paper_trading|aitos' || true
echo; echo '--- AITOS health endpoint ---'
if command -v curl >/dev/null 2>&1; then
  health_body="$(mktemp)"; health_code="000"; if health_code="$(curl --silent --show-error --max-time 5 -o "$health_body" -w '%{http_code}' "$HEALTH_URL" 2>/dev/null)"; then :; else health_code="000"; fi
  echo "Health HTTP status: $health_code"; echo 'Health response body:'; cat "$health_body" || true; echo
  if [ "$health_code" = "200" ]; then echo 'Health endpoint: PASS'; else echo 'Health endpoint: FAIL/unreachable'; blockers=$((blockers + 1)); fi; rm -f "$health_body"; echo
  echo '--- AITOS metrics endpoint ---'; metrics_body="$(mktemp)"; metrics_code="000"; if metrics_code="$(curl --silent --show-error --max-time 5 -o "$metrics_body" -w '%{http_code}' "$METRICS_URL" 2>/dev/null)"; then :; else metrics_code="000"; fi; echo "Metrics HTTP status: $metrics_code"; head -n 40 "$metrics_body" || true; echo; [ "$metrics_code" = "200" ] && echo 'Metrics endpoint: PASS' || { echo 'Metrics endpoint: FAIL/unreachable'; blockers=$((blockers + 1)); }; rm -f "$metrics_body"
else echo 'curl: NOT INSTALLED; skipped health/metrics checks'; blockers=$((blockers + 1)); fi

echo; echo '=== Audit result ==='; if [ "$blockers" -eq 0 ]; then echo 'PASS: no production runtime blockers detected.'; else echo "FAIL: $blockers production runtime blocker(s) detected."; exit 1; fi
