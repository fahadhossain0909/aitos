#!/usr/bin/env bash
set -euo pipefail

ROOT="${AITOS_ROOT_CAUSE_REPORT_DIR:-$HOME/aitos-root-cause-forensics}"
DURATION="${AITOS_ROOT_CAUSE_DURATION_SECONDS:-180}"
INTERVAL="${AITOS_ROOT_CAUSE_INTERVAL_SECONDS:-10}"
rm -rf "$ROOT"
mkdir -p "$ROOT/snapshots"
start="$(date +%s)"
i=0
while :; do
  now="$(date +%s)"
  elapsed=$((now-start))
  snap="$ROOT/snapshots/$(printf '%04d' "$i")"
  mkdir -p "$snap"
  AITOS_ROOT_CAUSE_REPORT_DIR="$snap" bash scripts/root_cause_forensics.sh >/dev/null
  printf '%s\t%s\n' "$now" "$snap" >> "$ROOT/timeline.tsv"
  i=$((i+1))
  (( elapsed >= DURATION )) && break
  sleep "$INTERVAL"
done

python3 - "$ROOT" <<'PY'
import json, pathlib
root=pathlib.Path(__import__('sys').argv[1])
lines=[]
for row in sorted((root/'timeline.tsv').read_text().splitlines()):
    ts,snap=row.split('\t',1)
    p=pathlib.Path(snap)/'health.json'
    if not p.exists(): continue
    try: d=json.loads(p.read_text())
    except Exception: continue
    blob=json.dumps(d).lower()
    def find_numbers(x, wanted):
        out=[]
        def walk(v,path=''):
            if isinstance(v,dict):
                for k,z in v.items():
                    p=f'{path}.{k}' if path else k
                    if k.lower() in wanted and isinstance(z,(int,float)): out.append((p,z))
                    walk(z,p)
            elif isinstance(v,list):
                for j,z in enumerate(v): walk(z,f'{path}[{j}]')
        walk(x)
        return out
    metrics=find_numbers(d, {'freshness_drops','reconnect_count','idle_timeouts','errors','rejected','queue_depth','processed','batches'})
    lines.append({'timestamp':int(ts),'snapshot':snap,'metrics':dict(metrics),'has_redis': 'redis' in blob,'has_persistence':'persistence' in blob,'has_orderbook':'orderbook' in blob})
(root/'timeline.json').write_text(json.dumps(lines,indent=2))
summary=root/'timeline_summary.md'
with summary.open('w') as f:
    f.write('# Unified Root-Cause Forensics Timeline\n\n')
    f.write(f'Snapshots: {len(lines)}\n\n')
    for item in lines:
        f.write(f"## {item['timestamp']} — {pathlib.Path(item['snapshot']).name}\n")
        for k,v in item['metrics'].items(): f.write(f'- `{k}`: {v}\n')
        f.write('\n')
PY
cp "$ROOT/snapshots/$(basename "$(tail -1 "$ROOT/timeline.tsv" | cut -f2)")/root_cause_report.md" "$ROOT/latest_snapshot_report.md"
cat "$ROOT/timeline_summary.md"
