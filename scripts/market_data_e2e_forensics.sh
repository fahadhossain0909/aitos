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
STREAM_LENGTHS="$REPORT_DIR/stream_lengths.tsv"
: > "$SAMPLES"; : > "$STREAM_LENGTHS"
redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }
health() { curl -fsS --max-time 5 http://127.0.0.1:8090/health; }
printf '# AITOS Market Data E2E Forensic Report\n\n' > "$REPORT"
printf '%s\n' "- Started UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$REPORT"
printf '%s\n' "- Observation window: ${WINDOW_SECONDS}s" >> "$REPORT"
printf '%s\n\n' "- Sampling interval: ${INTERVAL_SECONDS}s" >> "$REPORT"
printf '%s\n\n' '## Data lineage' >> "$REPORT"
printf '%s\n' 'Exchange → WebSocket → Parser/Canonical Runtime → Redis/EventBus → Scanner/State → Persistence' >> "$REPORT"
for c in "$REDIS_CONTAINER" "$PAPER_CONTAINER"; do docker inspect "$c" >/dev/null 2>&1 || { echo "BLOCKER: missing container: $c" >> "$REPORT"; exit 1; }; done
end=$(( $(date +%s) + WINDOW_SECONDS ))
while [ "$(date +%s)" -lt "$end" ]; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if payload=$(health 2>/dev/null); then
    HEALTH_PAYLOAD="$payload" TS="$ts" python3 -c 'import json,os; print(json.dumps({"ts":os.environ["TS"],"health":json.loads(os.environ["HEALTH_PAYLOAD"])}))' >> "$SAMPLES" || true
  else
    printf '%s\n' "{\"ts\":\"$ts\",\"health_error\":true}" >> "$SAMPLES"
  fi
  for key in stream:market.trade stream:market.book.snapshot stream:market.book.delta stream:market.ticker stream:market.kline stream:market.funding stream:market.open_interest stream:market.liquidation stream:market.instrument; do
    if [ "$(redis EXISTS "$key" 2>/dev/null || echo 0)" = 1 ]; then printf '%s\t%s\t%s\n' "$ts" "$key" "$(redis XLEN "$key" 2>/dev/null || echo 0)" >> "$STREAM_LENGTHS"; fi
  done
  sleep "$INTERVAL_SECONDS"
done
SAMPLES="$SAMPLES" STREAM_LENGTHS="$STREAM_LENGTHS" REDIS_CONTAINER="$REDIS_CONTAINER" REPORT="$REPORT" JSON="$JSON" python3 <<'PY'
import json, os, subprocess
samples=[]
for line in open(os.environ['SAMPLES'],encoding='utf-8'):
    try: samples.append(json.loads(line))
    except Exception: pass
streams={}
for line in open(os.environ['STREAM_LENGTHS'],encoding='utf-8'):
    ts,key,n=line.rstrip().split('\t'); streams.setdefault(key,[]).append((ts,int(n)))
last=samples[-1].get('health',{}) if samples else {}
# /health returns {status, modules:[{module_id, details}, ...]}; older builds exposed details directly.
details=last.get('details',{}) if isinstance(last,dict) else {}
modules=last.get('modules',[]) if isinstance(last,dict) else []
module_details={m.get('module_id'):m.get('details',{}) for m in modules if isinstance(m,dict)}
ingestion=module_details.get('data-ingestion-service',{})
canonical=details.get('canonical_market_data') or last.get('canonical_market_data') or ingestion.get('canonical_market_data') or {}
deep=details.get('deep_market_data') or last.get('deep_market_data') or ingestion.get('deep_market_data') or {}
persist=details.get('canonical_persistence') or last.get('canonical_persistence') or ingestion.get('canonical_persistence') or {}
transport=details.get('transport_telemetry') or last.get('transport_telemetry') or ingestion.get('transport_telemetry') or {}
h=canonical.get('health',{}) if isinstance(canonical,dict) else {}
checks=[]
def add(name,status,evidence): checks.append({'name':name,'status':status,'evidence':evidence})
if not ingestion: add('data-ingestion health exposed','FAIL','module data-ingestion-service missing from /health')
else: add('data-ingestion health exposed','PASS','data-ingestion-service module found')
if not canonical: add('canonical websocket telemetry exposed','FAIL','canonical_market_data missing from data-ingestion-service details')
else:
    state=canonical.get('state'); add('canonical websocket state','PASS' if state=='connected' and h.get('connected') is True else ('FAIL' if state in ('degraded','reconnecting','connecting') else 'WARN'),state)
    for m in ('received_events','accepted_events','published_events'):
        v=h.get(m); add(m,'PASS' if isinstance(v,int) and v>0 else 'FAIL',v)
    for m in ('publish_errors','dropped_events','stale_events','decode_errors','sequence_errors'):
        v=h.get(m); add(m,'PASS' if v in (0,None) else 'FAIL',v)
    age=h.get('receive_to_now_age_ms'); add('latest receive freshness','PASS' if isinstance(age,(int,float)) and age<15000 else ('FAIL' if age is not None else 'FAIL'),age)
summary={}
for key,vals in streams.items():
    s={'first_xlen':vals[0][1],'last_xlen':vals[-1][1],'delta':vals[-1][1]-vals[0][1],'samples':len(vals)}; summary[key]=s
    add(f'{key} growth','PASS' if s['delta']>0 else 'FAIL',s)
def rc(*args):
    try: return subprocess.check_output(['docker','exec',os.environ['REDIS_CONTAINER'],'redis-cli',*args],text=True,stderr=subprocess.DEVNULL).strip()
    except Exception: return ''
groups={}
for key in ('stream:market.trade','stream:market.book.snapshot'):
    raw=rc('XINFO','GROUPS',key)
    if raw:
        toks=raw.splitlines(); parsed=[]; i=0
        while i+1<len(toks):
            if toks[i]=='name':
                item={'name':toks[i+1]}
                for j in range(i+2,min(i+24,len(toks)-1),2): item[toks[j]]=toks[j+1]
                parsed.append(item)
            i+=1
        groups[key]=parsed
result={'checks':checks,'health':{'canonical_market_data':canonical,'deep_market_data':deep,'canonical_persistence':persist,'transport_telemetry':transport},'streams':summary,'consumer_groups':groups,'samples':len(samples),'health_module_ids':sorted(module_details)}
json.dump(result,open(os.environ['JSON'],'w',encoding='utf-8'),indent=2,sort_keys=True)
with open(os.environ['REPORT'],'a',encoding='utf-8') as f:
    f.write('\n## Final telemetry snapshot\n\n```json\n'+json.dumps(result['health'],indent=2,sort_keys=True)+'\n```\n')
    f.write('\n## Redis stream growth\n\n| Stream | First XLEN | Last XLEN | Delta |\n|---|---:|---:|---:|\n')
    for k,s in summary.items(): f.write(f'| `{k}` | {s["first_xlen"]} | {s["last_xlen"]} | {s["delta"]} |\n')
    f.write('\n## Checks\n\n| Check | Status | Evidence |\n|---|---|---|\n')
    for c in checks: f.write(f'| {c["name"]} | **{c["status"]}** | `{str(c["evidence"]).replace("|","/")}` |\n')
    f.write('\n## Consumer groups\n\n```json\n'+json.dumps(groups,indent=2,sort_keys=True)+'\n```\n')
failed=[c for c in checks if c['status']=='FAIL']
if failed: raise SystemExit(f'FAIL: {len(failed)} forensic checks failed')
if not samples: raise SystemExit('FAIL: no health samples collected')
PY
{
  echo; echo '## Container state'; docker compose ps 2>&1 || true
  echo; echo '## Recent market-data telemetry'; docker logs --since "${WINDOW_SECONDS}s" --timestamps "$PAPER_CONTAINER" 2>&1 | grep -E 'market-data websocket receive lag|trade source/parser attribution|depth source/parser attribution|live state trade processing latency|redis xadd latency|event-loop scheduling lag|live scanner freshness|paper signal diagnostics|trade transport switched|trade transport recovered|filtered stale REST' | tail -n 300 || true
} >> "$REPORT"
printf 'Generated: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$REPORT_DIR/VERDICT"
