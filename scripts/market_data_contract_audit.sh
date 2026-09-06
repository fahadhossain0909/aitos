#!/usr/bin/env bash
set -euo pipefail

REDIS_CONTAINER="${REDIS_CONTAINER:-aitos-redis}"
PAPER_CONTAINER="${PAPER_CONTAINER:-aitos-paper}"
MAX_PENDING="${AITOS_MAX_PENDING:-100}"
MAX_THROTTLED_RATIO="${AITOS_MAX_THROTTLED_RATIO:-0.25}"
blockers=0
fail() { echo "BLOCKER: $*"; blockers=$((blockers + 1)); }
warn() { echo "WARNING: $*"; }

printf '=== AITOS Market-Data Contract Audit ===\nUTC: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

for c in "$REDIS_CONTAINER" "$PAPER_CONTAINER"; do
  if ! docker inspect "$c" >/dev/null 2>&1; then
    fail "missing container: $c"
    continue
  fi
  status="$(docker inspect --format '{{.State.Status}}' "$c")"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c")"
  echo "$c status=$status health=$health"
  [ "$status" = running ] || fail "$c is not running"
  [ "$health" = healthy ] || warn "$c health is $health"
done

echo '--- Redis consumer-group pending entries ---'
if docker inspect "$REDIS_CONTAINER" >/dev/null 2>&1; then
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    docker exec "$REDIS_CONTAINER" redis-cli --json XINFO GROUPS "$key" 2>/dev/null | \
      MAX_PENDING="$MAX_PENDING" python3 -c 'import json,os,sys
for g in json.load(sys.stdin):
    pending=int(g.get("pending",0)); name=g.get("name","")
    print(f"{name}: pending={pending} lag={g.get(\"lag\")}")
    if pending > int(os.environ["MAX_PENDING"]): raise SystemExit(2)' || {
      rc=$?
      [ "$rc" -eq 2 ] && fail "consumer-group pending exceeds ${MAX_PENDING}: $key"
    }
  done < <(docker exec "$REDIS_CONTAINER" redis-cli --raw --scan --pattern 'stream:market.*' 2>/dev/null || true)
fi

echo '--- Redis memory / command pressure ---'
redis_stats="$(docker exec "$REDIS_CONTAINER" redis-cli INFO stats 2>/dev/null || true)"
redis_memory="$(docker exec "$REDIS_CONTAINER" redis-cli INFO memory 2>/dev/null || true)"
printf '%s\n' "$redis_stats" | grep -E '^(instantaneous_ops_per_sec:|total_error_replies:|evicted_keys:|rejected_connections:)' || true
printf '%s\n' "$redis_memory" | grep -E '^(used_memory_human:|used_memory_rss_human:|maxmemory_human:|mem_fragmentation_ratio:)' || true

echo '--- Docker CPU throttling ---'
for c in "$REDIS_CONTAINER" "$PAPER_CONTAINER"; do
  stats="$(docker exec "$c" sh -c 'cat /sys/fs/cgroup/cpu.stat 2>/dev/null || cat /sys/fs/cgroup/cpu/cpu.stat 2>/dev/null' || true)"
  if [ -n "$stats" ]; then
    periods="$(printf '%s\n' "$stats" | awk '$1=="nr_periods"{print $2}')"
    throttled="$(printf '%s\n' "$stats" | awk '$1=="nr_throttled"{print $2}')"
    ratio="$(awk -v t="${throttled:-0}" -v p="${periods:-0}" 'BEGIN{if(p>0) printf "%.4f",t/p; else print "0"}')"
    echo "$c cpu periods=${periods:-0} throttled=${throttled:-0} ratio=$ratio"
    awk -v r="$ratio" -v m="$MAX_THROTTLED_RATIO" 'BEGIN{exit !(r>m)}' && fail "$c CPU throttling ratio $ratio exceeds $MAX_THROTTLED_RATIO" || true
  else
    warn "$c CPU cgroup telemetry unavailable"
  fi
done

echo '--- Canonical application health ---'
if curl -fsS --max-time 5 http://127.0.0.1:8090/health >/tmp/aitos-health.json 2>/dev/null; then
  python3 - <<'PY'
import json
p='/tmp/aitos-health.json'
data=json.load(open(p))
canonical=data.get('details',{}).get('canonical_market_data') or data.get('canonical_market_data')
if not canonical:
    print('WARNING: canonical market-data health not exposed by /health')
else:
    h=canonical.get('health',{})
    print(json.dumps(h, indent=2, sort_keys=True))
    if h.get('receive_to_now_age_ms') is None:
        raise SystemExit('missing canonical receive telemetry')
PY
else
  fail 'paper health endpoint is unavailable'
fi
rm -f /tmp/aitos-health.json
printf '\nRESULT: blockers=%s\n' "$blockers"
[ "$blockers" -eq 0 ]
