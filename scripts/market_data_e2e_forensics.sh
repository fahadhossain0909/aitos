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
HEALTH_RAW="$REPORT_DIR/health_raw.jsonl"
STREAM_LENGTHS="$REPORT_DIR/stream_lengths.tsv"
STREAM_INFO="$REPORT_DIR/stream_info.txt"
GROUP_INFO="$REPORT_DIR/consumer_groups.txt"
: > "$SAMPLES"; : > "$HEALTH_RAW"; : > "$STREAM_LENGTHS"; : > "$STREAM_INFO"; : > "$GROUP_INFO"

redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }
health() { curl -fsS --max-time 5 http://127.0.0.1:8090/health; }

printf '# AITOS Market Data E2E Forensic Report\n\n' > "$REPORT"
printf '%s\n' "- Started UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$REPORT"
printf '%s\n' "- Observation window: ${WINDOW_SECONDS}s" >> "$REPORT"
printf '%s\n\n' "- Sampling interval: ${INTERVAL_SECONDS}s" >> "$REPORT"
printf '%s\n\n' '## Data lineage' >> "$REPORT"
printf '%s\n' 'Exchange → WebSocket → Parser/Canonical Runtime → Redis/EventBus → Scanner/State → Persistence' >> "$REPORT"

for c in "$REDIS_CONTAINER" "$PAPER_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "BLOCKER: missing container: $c" >> "$REPORT"; exit 1; }
done

end=$(( $(date +%s) + WINDOW_SECONDS ))
while [ "$(date +%s)" -lt "$end" ]; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if payload=$(health 2>/dev/null); then
    printf '%s\n' "$payload" >> "$HEALTH_RAW"
    printf '%s\n' "$payload" | TS="$ts" python3 -c '
import json, os, sys
try:
    payload = json.load(sys.stdin)
    print(json.dumps({"ts": os.environ["TS"], "health": payload}, separators=(",", ":")))
except Exception as exc:
    print(json.dumps({"ts": os.environ["TS"], "health_parse_error": str(exc)}))
' >> "$SAMPLES" || true
  else
    printf '%s\n' "{\"ts\":\"$ts\",\"health_error\":true}" >> "$SAMPLES"
  fi

  for key in stream:market.trade stream:market.book.snapshot stream:market.book.delta stream:market.ticker stream:market.kline stream:market.funding stream:market.open_interest stream:market.liquidation stream:market.instrument; do
    if [ "$(redis EXISTS "$key" 2>/dev/null || echo 0)" = 1 ]; then
      printf '%s\t%s\t%s\n' "$ts" "$key" "$(redis XLEN "$key" 2>/dev/null || echo 0)" >> "$STREAM_LENGTHS"
    fi
  done
  sleep "$INTERVAL_SECONDS"
done

for key in stream:market.trade stream:market.book.snapshot stream:market.book.delta stream:market.ticker stream:market.kline stream:market.funding stream:market.open_interest stream:market.liquidation stream:market.instrument; do
  if [ "$(redis EXISTS "$key" 2>/dev/null || echo 0)" = 1 ]; then
    { echo "### $key"; redis XINFO STREAM "$key" || true; } >> "$STREAM_INFO"
  fi
done
for key in stream:market.trade stream:market.book.snapshot stream:market.book.delta stream:market.ticker stream:market.kline stream:market.funding stream:market.open_interest stream:market.liquidation stream:market.instrument; do
  if [ "$(redis EXISTS "$key" 2>/dev/null || echo 0)" = 1 ]; then
    { echo "### $key"; redis XINFO GROUPS "$key" || true; } >> "$GROUP_INFO"
  fi
done

SAMPLES="$SAMPLES" STREAM_LENGTHS="$STREAM_LENGTHS" REDIS_CONTAINER="$REDIS_CONTAINER" REPORT="$REPORT" JSON="$JSON" python3 <<'PY'
import json
import os
import subprocess


def load_samples(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out

samples = load_samples(os.environ["SAMPLES"])
streams = {}
with open(os.environ["STREAM_LENGTHS"], encoding="utf-8") as fh:
    for line in fh:
        try:
            ts, key, n = line.rstrip().split("\t")
            streams.setdefault(key, []).append((ts, int(n)))
        except ValueError:
            continue

last = samples[-1].get("health", {}) if samples else {}
if not isinstance(last, dict):
    last = {}
details = last.get("details", {})
if not isinstance(details, dict):
    details = {}
modules = last.get("modules", [])
if not isinstance(modules, list):
    modules = []
module_details = {
    m.get("module_id"): m.get("details", {})
    for m in modules
    if isinstance(m, dict) and isinstance(m.get("details", {}), dict)
}
ingestion = module_details.get("data-ingestion-service", {})
if not isinstance(ingestion, dict):
    ingestion = {}

canonical = details.get("canonical_market_data") or last.get("canonical_market_data") or ingestion.get("canonical_market_data") or {}
deep = details.get("deep_market_data") or last.get("deep_market_data") or ingestion.get("deep_market_data") or {}
persist = details.get("canonical_persistence") or last.get("canonical_persistence") or ingestion.get("canonical_persistence") or {}
transport = details.get("transport_telemetry") or last.get("transport_telemetry") or ingestion.get("transport_telemetry") or {}

h = canonical.get("health", {}) if isinstance(canonical, dict) else {}
checks = []
def add(name, status, evidence):
    checks.append({"name": name, "status": status, "evidence": evidence})

if ingestion:
    add("data-ingestion health exposed", "PASS", "data-ingestion-service module found")
else:
    add("data-ingestion health exposed", "FAIL", "module data-ingestion-service missing from /health")

if canonical:
    state = canonical.get("state")
    if state == "connected" and h.get("connected") is True:
        state_status = "PASS"
    elif state in ("degraded", "reconnecting", "connecting"):
        state_status = "FAIL"
    else:
        state_status = "WARN"
    add("canonical websocket telemetry exposed", "PASS", "canonical_market_data found")
    add("canonical websocket state", state_status, state)
    for metric in ("received_events", "accepted_events", "published_events"):
        value = h.get(metric)
        add(metric, "PASS" if isinstance(value, int) and value > 0 else "FAIL", value)
    for metric in ("publish_errors", "dropped_events", "stale_events", "decode_errors", "sequence_errors"):
        value = h.get(metric)
        add(metric, "PASS" if value in (0, None) else "FAIL", value)
    age = h.get("receive_to_now_age_ms")
    add("latest receive freshness", "PASS" if isinstance(age, (int, float)) and age < 15000 else "FAIL", age)
else:
    add("canonical websocket telemetry exposed", "FAIL", "canonical_market_data missing from /health")

# A bounded Redis stream can remain at the same XLEN while new entries are
# continuously appended and old entries are trimmed. Therefore delta == 0 is
# inconclusive, not a dead-stream failure. Consumer-group lag/PENDING and the
# transport/publish counters are the actual liveness evidence.
summary = {}
for key, vals in streams.items():
    first = vals[0][1]
    last_value = vals[-1][1]
    delta = last_value - first
    if delta > 0:
        status = "PASS"
    elif delta < 0:
        status = "WARN"
    else:
        status = "WARN"
    summary[key] = {"first_xlen": first, "last_xlen": last_value, "delta": delta, "samples": len(vals)}
    add(f"{key} growth", status, summary[key])


def rc(*args):
    try:
        return subprocess.check_output(
            ["docker", "exec", os.environ["REDIS_CONTAINER"], "redis-cli", *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""

groups = {}
for key in summary:
    raw = rc("XINFO", "GROUPS", key)
    if not raw:
        continue
    toks = raw.splitlines()
    parsed = []
    i = 0
    while i + 1 < len(toks):
        if toks[i] == "name":
            item = {"name": toks[i + 1]}
            j = i + 2
            while j + 1 < len(toks) and j < i + 24:
                item[toks[j]] = toks[j + 1]
                j += 2
            parsed.append(item)
        i += 1
    groups[key] = parsed

result = {
    "checks": checks,
    "health": {
        "canonical_market_data": canonical,
        "deep_market_data": deep,
        "canonical_persistence": persist,
        "transport_telemetry": transport,
    },
    "streams": summary,
    "consumer_groups": groups,
    "samples": len(samples),
    "health_module_ids": sorted(k for k in module_details if k),
}

with open(os.environ["JSON"], "w", encoding="utf-8") as fh:
    json.dump(result, fh, indent=2, sort_keys=True)

with open(os.environ["REPORT"], "a", encoding="utf-8") as fh:
    fh.write("\n## Final telemetry snapshot\n\n```json\n")
    fh.write(json.dumps(result["health"], indent=2, sort_keys=True))
    fh.write("\n```\n")
    fh.write("\n## Redis stream growth\n\n| Stream | First XLEN | Last XLEN | Delta | Samples |\n|---|---:|---:|---:|---:|\n")
    for key, item in summary.items():
        fh.write(f'| `{key}` | {item["first_xlen"]} | {item["last_xlen"]} | {item["delta"]} | {item["samples"]} |\n')
    fh.write("\n## Checks\n\n| Check | Status | Evidence |\n|---|---|---|\n")
    for check in checks:
        evidence = str(check["evidence"]).replace("|", "/")
        fh.write(f'| {check["name"]} | **{check["status"]}** | `{evidence}` |\n')
    fh.write("\n## Consumer groups\n\n```json\n")
    fh.write(json.dumps(groups, indent=2, sort_keys=True))
    fh.write("\n```\n")
    fh.write("\n## Evidence files\n\n- `health_raw.jsonl` — raw /health payloads\n- `stream_info.txt` — Redis XINFO STREAM metadata\n- `consumer_groups.txt` — Redis XINFO GROUPS metadata\n")

if not samples:
    raise SystemExit("FAIL: no health samples collected")
PY

{
  echo; echo '## Container state'; docker compose ps 2>&1 || true
  echo; echo '## Recent market-data telemetry'; docker logs --since "${WINDOW_SECONDS}s" --timestamps "$PAPER_CONTAINER" 2>&1 | grep -E 'market-data websocket receive lag|trade source/parser attribution|depth source/parser attribution|live state trade processing latency|redis xadd latency|event-loop scheduling lag|live scanner freshness|paper signal diagnostics|trade transport switched|trade transport recovered|filtered stale REST' | tail -n 300 || true
} >> "$REPORT"
printf 'Generated: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$REPORT_DIR/VERDICT"