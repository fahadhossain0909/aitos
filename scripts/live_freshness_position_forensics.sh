#!/usr/bin/env bash
set -euo pipefail

OUT="${AITOS_FORENSIC_OUT:-$HOME/aitos-live-freshness-position-forensics}"
WINDOW_MINUTES="${AITOS_FORENSIC_WINDOW_MINUTES:-30}"
PAPER_CONTAINER="${PAPER_CONTAINER:-aitos-paper}"
mkdir -p "$OUT"

LOG="$OUT/paper_logs.txt"
HEALTH="$OUT/health.json"
TIMELINE="$OUT/timeline.tsv"
REPORT="$OUT/report.md"

: > "$LOG"

docker logs --since "${WINDOW_MINUTES}m" --timestamps "$PAPER_CONTAINER" 2>&1 > "$LOG" || true
curl -fsS --max-time 5 http://127.0.0.1:8090/health > "$HEALTH" 2>/dev/null || printf '{}\n' > "$HEALTH"

python3 - "$LOG" "$HEALTH" "$TIMELINE" "$REPORT" "$WINDOW_MINUTES" <<'PY'
from __future__ import annotations
import json, re, sys
from collections import Counter
from datetime import datetime, timezone

log_path, health_path, timeline_path, report_path, window_minutes = sys.argv[1:]
lines = open(log_path, errors="replace").read().splitlines()
try:
    health = json.load(open(health_path))
except Exception:
    health = {}

PATTERNS = [
    ("ws_idle_timeout", re.compile(r"idle.?timeout|watchdog.*idle|stream.*idle", re.I)),
    ("ws_reconnect", re.compile(r"reconnect|reconnecting|websocket.*closed|websocket.*close", re.I)),
    ("ws_error", re.compile(r"websocket.*error|transport.*error|stream.*error", re.I)),
    ("rest_fallback", re.compile(r"trade transport switched to REST fallback|rest_fallback", re.I)),
    ("ws_recovered", re.compile(r"trade transport recovered to websocket|recovered.*websocket", re.I)),
    ("scanner_freshness", re.compile(r"live scanner freshness|paper signal diagnostics", re.I)),
    ("position_update", re.compile(r"POSITION_MONITOR market bridge delivered live price|lifecycle root-cause telemetry", re.I)),
    ("position_subscription", re.compile(r"position market-data subscription telemetry", re.I)),
    ("exit_eval", re.compile(r"exit.*evaluat|POSITION_MONITOR:|exit intelligence", re.I)),
    ("position_close", re.compile(r"position.*closed|trade.*closed|lifecycle.*close", re.I)),
]

def ts_of(line: str) -> str:
    m = re.match(r"^(\d{4}-\d\d-\d\d[T ][^ Z]+Z?)", line)
    return m.group(1) if m else ""

def parse_dt(value: str):
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

events=[]
for idx, line in enumerate(lines, 1):
    kinds=[name for name,rx in PATTERNS if rx.search(line)]
    if not kinds:
        continue
    events.append((ts_of(line), idx, ",".join(kinds), line[:1000]))

with open(timeline_path, "w") as f:
    f.write("timestamp\tline\ttype\tmessage\n")
    for row in events:
        f.write("\t".join(map(str, row)).replace("\n", " ") + "\n")

counts=Counter()
for _,_,types,_ in events:
    counts.update(types.split(","))

chains=[]
for i,(ts,line_no,types,msg) in enumerate(events):
    if "rest_fallback" not in types:
        continue
    previous=events[max(0,i-8):i]
    following=events[i:min(len(events),i+9)]
    chains.append({
        "fallback": (ts,line_no),
        "previous": [(a,b,c) for a,b,c,_ in previous],
        "following": [(a,b,c) for a,b,c,_ in following],
    })

position_times=[]
for ts, line_no, types, msg in events:
    if "position_update" in types:
        dt=parse_dt(ts)
        if dt: position_times.append((dt,ts,line_no))
gaps=[]
for a,b in zip(position_times,position_times[1:]):
    sec=(b[0]-a[0]).total_seconds()
    if sec >= 5:
        gaps.append((sec,a[1],b[1]))

def find_values(obj, wanted):
    out=[]
    def walk(x,path=""):
        if isinstance(x,dict):
            for k,v in x.items():
                p=f"{path}.{k}" if path else k
                if k.lower() in wanted:
                    out.append((p,v))
                walk(v,p)
        elif isinstance(x,list):
            for n,v in enumerate(x): walk(v,f"{path}[{n}]")
    walk(obj)
    return out

wanted={"reconnect_count","stream_idle_timeouts","orderbook","freshness_drops","transport_mode","transport_fallback_count","transport_recovery_count","transport_last_ws_event_at","transport_last_fallback_started_at"}
health_hits=find_values(health,wanted)

with open(report_path,"w") as f:
    f.write("# AITOS Live Freshness + Position Lifecycle Forensics\n\n")
    f.write(f"Observation window: last {window_minutes} minutes\n\n")
    f.write("## Evidence counts\n\n")
    for key in sorted(counts):
        f.write(f"- `{key}`: **{counts[key]}**\n")
    f.write("\n## Health evidence\n\n")
    if health_hits:
        for key,val in health_hits:
            f.write(f"- `{key}` = `{json.dumps(val, default=str)}`\n")
    else:
        f.write("- No matching health fields found. This is a telemetry gap, not evidence of absence.\n")
    f.write("\n## REST fallback incidents\n\n")
    if not chains:
        f.write("No REST fallback log event was observed in the window.\n")
    for n,chain in enumerate(chains,1):
        f.write(f"### Incident {n}\n")
        f.write(f"Fallback at `{chain['fallback'][0]}` (log line {chain['fallback'][1]}).\n\n")
        f.write("Nearby events:\n\n")
        for ts,ln,typ in chain["previous"] + chain["following"]:
            f.write(f"- `{ts}` line {ln}: `{typ}`\n")
        f.write("\n")
    f.write("## Position heartbeat gaps\n\n")
    if gaps:
        for sec,a,b in gaps:
            f.write(f"- **{sec:.1f}s gap** between position telemetry `{a}` and `{b}`. This does NOT prove a missed evaluation; correlate with `update_price` and exit-evaluation logs.\n")
    else:
        f.write("No >=5s gap was observed between explicit position telemetry events, or no position telemetry was emitted.\n")
    f.write("\n## First-divergence rules\n\n")
    f.write("1. `ws_idle_timeout` followed by reconnect with no transport-error evidence => classify as **data-path staleness candidate**, not proven socket failure.\n")
    f.write("2. `rest_fallback` without a preceding WS-staleness/reconnect event => investigate scanner/source selection directly.\n")
    f.write("3. Open position + subscription telemetry but no position-update telemetry => investigate market.trade/kline delivery.\n")
    f.write("4. Position-update telemetry present but no exit-evaluation evidence => investigate TradeLifecycle/PositionManager execution path.\n")
    f.write("5. Exit-evaluation evidence present but no close/fill evidence => investigate execution/reconciliation.\n")
    f.write("\n## Important limitation\n\n")
    f.write("This report never interprets silence as a HOLD decision. Runtime telemetry must explicitly record an evaluation before an exit decision is considered proven.\n")
PY

printf 'Forensics written to %s\n' "$OUT"
