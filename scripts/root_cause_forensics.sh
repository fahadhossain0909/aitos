#!/usr/bin/env bash
set -euo pipefail

REPORT_DIR="${AITOS_ROOT_CAUSE_REPORT_DIR:-$HOME/aitos-root-cause-forensics}"
mkdir -p "$REPORT_DIR"
REPORT="$REPORT_DIR/root_cause_report.md"
RAW="$REPORT_DIR/root_cause_raw.jsonl"
: > "$RAW"

redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }
REDIS_CONTAINER="${REDIS_CONTAINER:-}"
if [[ -z "$REDIS_CONTAINER" ]]; then
  REDIS_CONTAINER="$(docker ps --format '{{.Names}}' | grep -E '(^|[-_])redis($|[-_])' | head -1 || true)"
fi

printf '# AITOS Unified Root-Cause Forensics\n\n' > "$REPORT"
printf 'Generated: %s\n\n' "$(date -Is)" >> "$REPORT"

health_json=""
if curl -fsS --max-time 5 http://127.0.0.1:8090/health > "$REPORT_DIR/health.json"; then
  health_json="$(cat "$REPORT_DIR/health.json")"
  printf '%s\n' "$health_json" >> "$RAW"
else
  printf 'WARNING: health endpoint unavailable\n' >> "$REPORT"
fi

printf '## Host / container CPU evidence\n\n' >> "$REPORT"
if [[ -r /sys/fs/cgroup/cpu.stat ]]; then
  cat /sys/fs/cgroup/cpu.stat > "$REPORT_DIR/cgroup_cpu.stat"
  cat "$REPORT_DIR/cgroup_cpu.stat" >> "$REPORT"
  printf '\n' >> "$REPORT"
fi
if docker stats --no-stream --format '{{json .}}' > "$REPORT_DIR/docker_stats.jsonl" 2>/dev/null; then
  cat "$REPORT_DIR/docker_stats.jsonl" >> "$REPORT"
  printf '\n' >> "$REPORT"
fi

printf '## Redis consumer topology\n\n' >> "$REPORT"
if [[ -n "$REDIS_CONTAINER" ]]; then
  printf 'Redis container: `%s`\n\n' "$REDIS_CONTAINER" >> "$REPORT"
  redis --version > "$REPORT_DIR/redis_version.txt" 2>&1 || true
  {
    for stream in market.trade market.book.delta market.book.snapshot market.ticker market.funding market.open_interest market.liquidation market.options market.instrument; do
      key="stream:$stream"
      if redis EXISTS "$key" | grep -q '^1$'; then
        echo "### $key"
        echo '```'
        redis XINFO GROUPS "$key" 2>&1 || true
        echo '--- CONSUMERS ---'
        redis XINFO CONSUMERS "$key" "persistence" 2>&1 || true
        echo '```'
      fi
    done
  } > "$REPORT_DIR/redis_consumers.txt"
  cat "$REPORT_DIR/redis_consumers.txt" >> "$REPORT"
  printf '\n' >> "$REPORT"
else
  printf 'WARNING: Redis container not found.\n\n' >> "$REPORT"
fi

printf '## Application forensic evidence\n\n' >> "$REPORT"
if [[ -n "$health_json" ]]; then
  python3 - "$REPORT_DIR/health_summary.json" "$health_json" <<'PY'
import json, sys
out, raw = sys.argv[1:]
d=json.loads(raw)

def walk(x, path=''):
    if isinstance(x, dict):
        for k,v in x.items():
            p=f'{path}.{k}' if path else k
            if any(t in k.lower() for t in ('persistence','gateway','redis','orderbook','freshness','event_loop','transport','consumer')):
                print(p, '=', json.dumps(v, separators=(',', ':')))
            walk(v,p)
    elif isinstance(x,list):
        for i,v in enumerate(x): walk(v,f'{path}[{i}]')
with open(out,'w') as f:
    for line in walk(d) or []:
        f.write(line+'\n')
PY
  cat "$REPORT_DIR/health_summary.json" >> "$REPORT"
  printf '\n' >> "$REPORT"
fi

printf '## Recent application logs for transport / Redis / persistence failures\n\n' >> "$REPORT"
for c in $(docker ps --format '{{.Names}}' | grep -E '^aitos' || true); do
  echo "### $c" >> "$REPORT"
  docker logs --since 10m "$c" 2>&1 | grep -Ei 'orderbook|idle.?timeout|reconnect|redis|xadd|clickhouse|persist|timeout|connection|consumer|event.?loop' | tail -200 >> "$REPORT" || true
  printf '\n' >> "$REPORT"
done

printf '## Automated interpretation\n\n' >> "$REPORT"
python3 - "$REPORT_DIR/health.json" "$REPORT_DIR/diagnosis.json" <<'PY'
import json,sys
src,out=sys.argv[1:]
try: d=json.load(open(src))
except Exception:
 json.dump({'status':'health_unavailable'},open(out,'w')); raise SystemExit
blob=json.dumps(d).lower()
checks={
 'freshness': any(k in blob for k in ('freshness_drops','dropped_events')),
 'persistence': 'persistence' in blob,
 'redis': 'redis' in blob,
 'orderbook': 'orderbook' in blob,
 'event_loop': 'event_loop' in blob,
}
json.dump({'checks':checks},open(out,'w'),indent=2)
PY
cat "$REPORT_DIR/diagnosis.json" >> "$REPORT"

printf '\n## Interpretation rules\n\n' >> "$REPORT"
cat >> "$REPORT" <<'EOF'
- **Redis long-tail:** correlate gateway publisher p95/p99/max with Redis XADD latency and event-loop lag. A gateway stall without Redis latency points away from Redis.
- **Historical persistence:** compare enqueue/sec, processed/sec, queue-depth growth, rejection rate, actual batch-size distribution and ClickHouse write latency. Do not change worker count without this capacity comparison.
- **Redis consumers:** inspect consumer count, idle time, pending entries and stable-vs-stale names. A growing population of restart-generated consumers is a lifecycle defect, not a reason to add workers.
- **Orderbook transport:** correlate idle timeout → reconnect → first message latency → REST fallback activity → recovery. Distinguish a real exchange silence from a local reader/event-loop stall.
- **Flaky integration:** correlate repeated test outcomes and infrastructure/service readiness before changing application behavior.
- **CPU:** only classify CPU as causal when cgroup throttling or sustained container saturation overlaps the observed application stall.
EOF

printf '\nArtifacts written to %s\n' "$REPORT_DIR"
