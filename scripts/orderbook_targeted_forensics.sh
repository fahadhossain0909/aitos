#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${AITOS_ORDERBOOK_FORENSICS_OUTPUT:-$HOME/aitos-orderbook-targeted-forensics}"
WINDOW_SECONDS="${AITOS_ORDERBOOK_FORENSICS_WINDOW_SECONDS:-180}"
SAMPLE_SECONDS="${AITOS_ORDERBOOK_FORENSICS_SAMPLE_SECONDS:-5}"
mkdir -p "$OUTPUT_DIR"
REPORT="$OUTPUT_DIR/report.md"
RAW="$OUTPUT_DIR/raw"
mkdir -p "$RAW"

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

PAPER_CONTAINER="$(docker compose ps -q aitos-paper 2>/dev/null || true)"
if [[ -z "$PAPER_CONTAINER" ]]; then
  PAPER_CONTAINER="$(docker ps --filter 'name=aitos-paper' --format '{{.ID}}' | head -n1 || true)"
fi

log "container=${PAPER_CONTAINER:-not-found} window=${WINDOW_SECONDS}s sample=${SAMPLE_SECONDS}s"

{
  echo "# AITOS targeted order-book forensic pass"
  echo
  echo "- Started (UTC): $(date -u +%FT%TZ)"
  echo "- Window: ${WINDOW_SECONDS}s"
  echo "- Sample interval: ${SAMPLE_SECONDS}s"
  echo "- Paper container: ${PAPER_CONTAINER:-not-found}"
  echo
  echo "## Purpose"
  echo "Distinguish Binance transport silence, sequence/bootstrap churn, queue pressure, and LocalOrderBook processing cost before changing order-book semantics."
} > "$REPORT"

# Baseline metadata and container health.
docker compose ps > "$RAW/compose_ps.txt" 2>&1 || true
docker inspect "$PAPER_CONTAINER" > "$RAW/paper_inspect.json" 2>&1 || true
if [[ -n "$PAPER_CONTAINER" ]]; then
  docker stats --no-stream "$PAPER_CONTAINER" > "$RAW/paper_stats_baseline.txt" 2>&1 || true
fi

# Capture application logs for the exact observation window. The log stream is the primary
# evidence for connect/handshake/first-frame/reconnect/sequence-error/orderbook watchdog events.
if [[ -n "$PAPER_CONTAINER" ]]; then
  timeout "$((WINDOW_SECONDS + 20))" docker logs -f --since 1s --timestamps "$PAPER_CONTAINER" > "$RAW/paper_logs.txt" 2>&1 &
  LOG_PID=$!
else
  LOG_PID=""
fi

# Sample container CPU/memory plus process-level evidence while the same log window runs.
SAMPLES=0
while (( SAMPLES * SAMPLE_SECONDS < WINDOW_SECONDS )); do
  ts="$(date -u +%FT%TZ)"
  {
    echo "### $ts"
    docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.MemPerc}} {{.NetIO}} {{.PIDs}}' 2>/dev/null | grep -E 'aitos-paper|aitos' || true
    if [[ -n "$PAPER_CONTAINER" ]]; then
      docker top "$PAPER_CONTAINER" -eo pid,ppid,pcpu,pmem,etime,args 2>/dev/null || true
    fi
  } >> "$RAW/runtime_samples.txt"
  ((SAMPLES+=1))
  sleep "$SAMPLE_SECONDS"
done

if [[ -n "${LOG_PID:-}" ]]; then
  wait "$LOG_PID" 2>/dev/null || true
fi

# Extract only order-book/transport signals so the report is readable while preserving raw logs.
grep -Ei 'order.?book|depth|sequence|bootstrap|idle|watchdog|Binance websocket|websocket.*(connect|connected|close|error|frame)|reconnect|queue' "$RAW/paper_logs.txt" > "$RAW/orderbook_signals.txt" || true

# Quantify the distinct failure modes. These counts are deliberately observational; no threshold
# is treated as a root cause by itself.
count() { grep -Eic "$1" "$RAW/paper_logs.txt" 2>/dev/null || true; }
CONNECTS=$(count 'Binance websocket connecting')
HANDSHAKES=$(count 'Binance websocket connected')
FIRST_FRAMES=$(count 'first frame')
SEQ_ERRORS=$(count 'OrderBookSequenceError|sequence.*break|chain break|does not bridge')
IDLE_EVENTS=$(count 'idle timeout|watchdog.*idle|order.?book.*idle')
RECONNECTS=$(count 'reconnect|reconnecting|websocket.*close|websocket.*closed')
ORDERBOOK_ERRORS=$(count 'order.?book.*(error|failed)|depth.*(error|failed)')
QUEUE_EVENTS=$(count 'queue.*(full|overflow|depth|backlog)|QueueFull')

{
  echo "## Observed signal counts"
  echo
  echo "| Signal | Count |"
  echo "|---|---:|"
  echo "| WebSocket connect attempts | $CONNECTS |"
  echo "| Successful handshakes | $HANDSHAKES |"
  echo "| First-frame events | $FIRST_FRAMES |"
  echo "| Sequence/bootstrap errors | $SEQ_ERRORS |"
  echo "| Idle/watchdog events | $IDLE_EVENTS |"
  echo "| Reconnect/close events | $RECONNECTS |"
  echo "| Order-book/depth errors | $ORDERBOOK_ERRORS |"
  echo "| Queue pressure events | $QUEUE_EVENTS |"
  echo
  echo "## Interpretation gates"
  echo
  echo "1. **Transport silence candidate:** handshake succeeds but first-frame events stop or reconnects occur without sequence errors."
  echo "2. **Sequence/bootstrap candidate:** repeated sequence/bridge errors followed by REST bootstrap/reconnect cycles."
  echo "3. **Queue/backpressure candidate:** QueueFull/overflow/backlog signals or rising queue-related latency while raw frames continue."
  echo "4. **Local reconstruction candidate:** raw frames continue and sequence remains healthy, but CPU/process cost or watchdog/idle symptoms rise; correlate with LocalOrderBook snapshot/apply telemetry if present."
  echo "5. **Unresolved:** if logs do not expose enough stage markers, do not change order-book semantics from this pass; add in-process stage telemetry next."
  echo
  echo "## Runtime samples"
  echo '```text'
  cat "$RAW/runtime_samples.txt"
  echo '```'
  echo
  echo "## Evidence files"
  echo "- orderbook_signals.txt: filtered order-book/transport events"
  echo "- paper_logs.txt: raw application log window"
  echo "- runtime_samples.txt: CPU/memory/process samples"
} >> "$REPORT"

# Non-zero only for missing container; the forensic report itself remains an artifact even when
# the live application has a transient failure.
if [[ -z "$PAPER_CONTAINER" ]]; then
  echo "" >> "$REPORT"
  echo "> **WARNING:** aitos-paper container was not found; transport/runtime evidence is incomplete." >> "$REPORT"
fi

cat "$REPORT"
