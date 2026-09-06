#!/usr/bin/env bash
set -euo pipefail

WINDOW_SECONDS="${AITOS_FORENSIC_WINDOW_SECONDS:-120}"
INTERVAL_SECONDS="${AITOS_FORENSIC_INTERVAL_SECONDS:-10}"
REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
PAPER_CONTAINER="${AITOS_PAPER_CONTAINER:-aitos-paper}"
REPORT_DIR="${AITOS_FORENSIC_REPORT_DIR:-$HOME/aitos-market-data-e2e-forensics}"
mkdir -p "$REPORT_DIR"
REPORT="$REPORT_DIR/report.md"
JSON="$REPORT_DIR/report.json"
SAMPLES="$REPORT_DIR/samples.jsonl"
: > "$SAMPLES"

json_health() {
  curl -fsS --max-time 5 http://127.0.0.1:8090/health
}
redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

cat > "$REPORT" <<EOF
# AITOS Market Data E2E Forensic Report

- Started UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)
- Observation window: ${WINDOW_SECONDS}s
- Sampling interval: ${INTERVAL_SECONDS}s

## Data lineage

Exchange → WebSocket → Parser/Canonical Runtime → Redis/EventBus → Scanner/State → Persistence

EOF

failures=0
warnings=0

if ! docker inspect "$REDIS_CONTAINER" >/dev/null 2>&1; then
  echo "BLOCKER: Redis container unavailable" >> "$REPORT"; failures=$((failures+1))
fi
if ! docker inspect "$PAPER_CONTAINER" >/dev/null 2>&1; then
  echo "BLOCKER: Paper container unavailable" >> "$REPORT"; failures=$((failures+1))
fi

START_NS=$(date +%s%N)
END_NS=$((START_NS + WINDOW_SECONDS * 1000000000))

while [ "$(date +%s%N)" -lt "$END_NS" ]; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  health='{}'
  if health=$(json_health 2>/dev/null); then
    printf '%s\n' "$health" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"ts":sys.argv[1],"health":d},separators=(",",":")))' "$ts" >> "$SAMPLES" || true
  else
    printf '%s\n' "{\"ts\":\"$ts\",\"health_error\":true}" >> "$SAMPLES"
  fi
  if docker inspect "$REDIS_CONTAINER" >/dev/null 2>&1; then
    for key in stream:market.trade stream:market.book.snapshot stream:market.book.delta stream:market.ticker stream:market.kline stream:market.funding stream:market.open_interest stream:market.liquidation stream:market.instrument; do
      exists=$(redis EXISTS "$key" 2>/dev/null || echo 0)
      if [ "$exists" = 1 ]; then
        len=$(redis XLEN "$key" 2>/dev/null || echo 0)
        printf '%s\t%s\t%s\n' "$ts" "$key" "$len" >> "$REPORT_DIR/stream_lengths.tsv"
      fi
    done
  fi
  sleep "$INTERVAL_SECONDS"
done

python3 - "$SAMPLES" "$REPORT_DIR/stream_lengths.tsv" "$JSON" "$REPORT" <<'PY'
import json, statistics, sys
from datetime import datetime, timezone
samples_path, lengths_path, out_json, out_md = sys.argv[1:]
samples=[]
for line in open(samples_path, encoding='utf-8'):
    try: samples.append(json.loads(line))
    except Exception: pass

streams={}
try:
    for line in open(lengths_path, encoding='utf-8'):
        ts,key,n=line.rstrip().split('\t'); streams.setdefault(key,[]).append((ts,int(n)))
except FileNotFoundError: pass

last_health=samples[-1].get('health',{}) if samples else {}
details=last_health.get('details',{}) if isinstance(last_health,dict) else {}
canonical=details.get('canonical_market_data') or last_health.get('canonical_market_data') or {}
deep=details.get('deep_market_data') or last_health.get('deep_market_data') or {}
persist=details.get('canonical_persistence') or last_health.get('canonical_persistence') or {}
transport=last_health.get('transport_telemetry') or details.get('transport_telemetry') or {}

health_obj={
    'canonical_market_data': canonical,
    'deep_market_data': deep,
    'canonical_persistence': persist,
    'transport_telemetry': transport,
}

stream_summary={}
for key,vals in streams.items():
    first=vals[0][1]; last=vals[-1][1]
    delta=last-first
    elapsed=max(1,len(vals)-1)
    stream_summary[key]={'first_xlen':first,'last_xlen':last,'delta':delta,'samples':len(vals)}

# Evidence-based checks. Missing telemetry is a warning, not silently a pass.
checks=[]
def check(name,status,evidence): checks.append({'name':name,'status':status,'evidence':evidence})

state=canonical.get('state')
health=canonical.get('health',{}) if isinstance(canonical,dict) else {}
if state == 'connected' and health.get('connected') is True: check('canonical websocket state','PASS',state)
elif state in ('degraded','reconnecting','connecting'): check('canonical websocket state','FAIL',state)
else: check('canonical websocket state','WARN',f'missing/unknown state: {state!r}')

for metric in ('received_events','accepted_events','published_events'):
    v=health.get(metric)
    check(metric, 'PASS' if isinstance(v,int) and v>0 else 'WARN', v)

for metric in ('publish_errors','dropped_events','stale_events','decode_errors','sequence_errors'):
    v=health.get(metric)
    check(metric, 'PASS' if v in (0,None) else 'FAIL', v)

age=health.get('receive_to_now_age_ms')
check('latest receive freshness','PASS' if isinstance(age,(int,float)) and age < 15000 else ('FAIL' if age is not None else 'WARN'), age)

for key,summary in stream_summary.items():
    check(f'{key} growth','PASS' if summary['delta']>0 else 'WARN',summary)

# Scanner PEL and DLQ are sampled directly from the final VPS state.
import subprocess

def redis_cmd(*args):
    try: return subprocess.check_output(['docker','exec', '${REDIS_CONTAINER}', 'redis-cli', *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception: return ''

pel={}
for key in ('stream:market.trade','stream:market.book.snapshot'):
    raw=redis_cmd('XINFO','GROUPS',key)
    if raw:
        # redis-cli default output is pairs; parse conservatively.
        toks=raw.splitlines()
        for i,t in enumerate(toks):
            if t=='name' and i+1<len(toks): pel.setdefault(key,[]).append({'name':toks[i+1]})

out={'checks':checks,'health':health_obj,'streams':stream_summary,'samples':len(samples),'pel_groups':pel}
json.dump(out,open(out_json,'w',encoding='utf-8'),indent=2,sort_keys=True)

with open(out_md,'a',encoding='utf-8') as f:
    f.write('\n## Final telemetry snapshot\n\n```json\n')
    f.write(json.dumps(health_obj,indent=2,sort_keys=True))
    f.write('\n```\n\n## Redis stream growth\n\n| Stream | First XLEN | Last XLEN | Delta |\n|---|---:|---:|---:|\n')
    for key,s in stream_summary.items(): f.write(f'| `{key}` | {s["first_xlen"]} | {s["last_xlen"]} | {s["delta"]} |\n')
    f.write('\n## Checks\n\n| Check | Status | Evidence |\n|---|---|---|\n')
    for c in checks:
        f.write(f'| {c["name"]} | **{c["status"]}** | `{str(c["evidence"]).replace("|","/")}` |\n')

bad=[c for c in checks if c['status']=='FAIL']
if bad:
    print(f'FAIL: {len(bad)} forensic checks failed')
    raise SystemExit(1)
if not samples:
    print('FAIL: no health samples collected')
    raise SystemExit(2)
print('PASS: end-to-end forensic observation completed')
PY

# Capture container/runtime evidence after the observation window.
{
  echo; echo '## Container state'; docker compose ps 2>&1 || true
  echo; echo '## Recent market-data telemetry'; docker logs --since "${WINDOW_SECONDS}s" --timestamps "$PAPER_CONTAINER" 2>&1 | grep -E 'market-data websocket receive lag|trade source/parser attribution|depth source/parser attribution|live state trade processing latency|redis xadd latency|event-loop scheduling lag|live scanner freshness|paper signal diagnostics|trade transport switched|trade transport recovered|filtered stale REST' | tail -n 300 || true
} >> "$REPORT"

cat > "$REPORT_DIR/VERDICT" <<EOF
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
See report.md and report.json for evidence.
EOF

echo "Report: $REPORT"
echo "JSON:   $JSON"
