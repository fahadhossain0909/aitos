#!/usr/bin/env bash
set -euo pipefail

REDIS_CONTAINER="${AITOS_REDIS_CONTAINER:-aitos-redis}"
ARCHIVE_CONTAINER="${AITOS_REDIS_ARCHIVE_CONTAINER:-aitos-redis-stream-archive}"
DLQ_CONTAINER="${AITOS_REDIS_DLQ_CONTAINER:-aitos-redis-dlq-retention}"
WINDOW_MINUTES="${AITOS_DIAGNOSTIC_LOG_MINUTES:-30}"
STREAM_SCAN_COUNT="${AITOS_REDIS_STREAM_SCAN_COUNT:-1000}"

printf '%s\n' '--- Redis pressure attribution snapshot ---'
printf 'Window: last %s minutes\n' "$WINDOW_MINUTES"

if ! docker inspect "$REDIS_CONTAINER" >/dev/null 2>&1; then
  echo 'BLOCKER: Redis container unavailable.'
  exit 1
fi

redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@"; }

printf '%s\n' '--- Redis container lifecycle / cgroup ---'
docker inspect "$REDIS_CONTAINER" --format '{{json .State}}' 2>/dev/null || true
docker inspect "$REDIS_CONTAINER" --format 'RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}} OOMKilled={{.State.OOMKilled}} Status={{.State.Status}}' 2>/dev/null || true
docker inspect "$REDIS_CONTAINER" --format 'CpuQuota={{.HostConfig.CpuQuota}} CpuPeriod={{.HostConfig.CpuPeriod}} NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}' 2>/dev/null || true

echo 'Redis cgroup files:'
docker exec "$REDIS_CONTAINER" sh -c '
  for f in /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.events; do
    if [ -r "$f" ]; then echo "### $f"; cat "$f"; fi
  done
' 2>/dev/null || true

echo 'Recent Redis container logs:'
docker logs --since "${WINDOW_MINUTES}m" --timestamps "$REDIS_CONTAINER" 2>&1 | tail -n 500 || true

printf '%s\n' '--- Redis INFO server/memory/persistence/stats/clients ---'
for section in server memory persistence stats clients cpu commandstats; do
  echo "### INFO $section"
  redis INFO "$section" 2>/dev/null || true
done

echo '### MEMORY STATS'
redis MEMORY STATS 2>/dev/null || true
echo '### MEMORY DOCTOR'
redis MEMORY DOCTOR 2>/dev/null || true

echo '### MEMORY MALLOC-STATS'
redis MEMORY MALLOC-STATS 2>/dev/null || true

echo '### INFO LATENCY (latest event state)'
redis LATENCY LATEST 2>/dev/null || true

echo '### LATENCY DOCTOR'
redis LATENCY DOCTOR 2>/dev/null || true

echo '### SLOWLOG LEN'
redis SLOWLOG LEN 2>/dev/null || true
echo '### SLOWLOG GET 128'
redis SLOWLOG GET 128 2>/dev/null || true

printf '%s\n' '--- Redis configuration relevant to pressure ---'
for pattern in maxmemory maxmemory-policy appendonly appendfsync auto-aof-rewrite-percentage auto-aof-rewrite-min-size aof-use-rdb-preamble hz dynamic-hz io-threads io-threads-do-reads repl-backlog-size stream-node-max-bytes stream-node-max-entries client-output-buffer-limit; do
  echo "### CONFIG $pattern"
  redis CONFIG GET "$pattern" 2>/dev/null || true
done

printf '%s\n' '--- Redis keyspace inventory ---'
redis DBSIZE 2>/dev/null || true
redis INFO keyspace 2>/dev/null || true

echo 'Top-level key type/size sample (SCAN only; no destructive operations):'
redis --raw --scan --count "$STREAM_SCAN_COUNT" 2>/dev/null | while IFS= read -r key; do
  [ -n "$key" ] || continue
  type="$(redis TYPE "$key" 2>/dev/null || echo unknown)"
  case "$type" in
    stream)
      len="$(redis XLEN "$key" 2>/dev/null || echo 0)"
      echo "STREAM $key XLEN=$len"
      ;;
    list)
      len="$(redis LLEN "$key" 2>/dev/null || echo 0)"
      echo "LIST $key LLEN=$len"
      ;;
    set)
      len="$(redis SCARD "$key" 2>/dev/null || echo 0)"
      echo "SET $key SCARD=$len"
      ;;
    zset)
      len="$(redis ZCARD "$key" 2>/dev/null || echo 0)"
      echo "ZSET $key ZCARD=$len"
      ;;
    hash)
      len="$(redis HLEN "$key" 2>/dev/null || echo 0)"
      echo "HASH $key HLEN=$len"
      ;;
    string)
      echo "STRING $key"
      ;;
    *)
      echo "OTHER $key TYPE=$type"
      ;;
  esac
done

printf '%s\n' '--- Stream inventory with exact lengths ---'
redis --raw --scan --pattern 'stream:*' --count "$STREAM_SCAN_COUNT" 2>/dev/null | sort | while IFS= read -r key; do
  [ -n "$key" ] || continue
  len="$(redis XLEN "$key" 2>/dev/null || echo 0)"
  first="$(redis --raw XRANGE "$key" - + COUNT 1 2>/dev/null | head -n 1 || true)"
  last="$(redis --raw XREVRANGE "$key" + - COUNT 1 2>/dev/null | head -n 1 || true)"
  echo "STREAM $key XLEN=$len FIRST=$first LAST=$last"
done

printf '%s\n' '--- Consumer-group pressure by stream ---'
redis --raw --scan --pattern 'stream:*' --count "$STREAM_SCAN_COUNT" 2>/dev/null | sort | while IFS= read -r key; do
  [ -n "$key" ] || continue
  echo "STREAM $key"
  redis --json XINFO GROUPS "$key" 2>/dev/null || true
  redis --json XINFO CONSUMERS "$key" '*' 2>/dev/null || true
done

printf '%s\n' '--- Pending-entry totals for every consumer group ---'
redis --raw --scan --pattern 'stream:*' --count "$STREAM_SCAN_COUNT" 2>/dev/null | sort | while IFS= read -r key; do
  [ -n "$key" ] || continue
  groups="$(redis --raw XINFO GROUPS "$key" 2>/dev/null | grep -E '^name$|^pending$' || true)"
  [ -n "$groups" ] || continue
  redis --json XINFO GROUPS "$key" 2>/dev/null | tr -d '\n' | sed 's/},{/}\n{/g' | while IFS= read -r group; do
    name="$(printf '%s' "$group" | sed -n 's/.*"name"[^\"]*"\([^\"]*\)".*/\1/p')"
    pending="$(printf '%s' "$group" | sed -n 's/.*"pending"[^0-9]*\([0-9][0-9]*\).*/\1/p')"
    [ -n "$name" ] || continue
    echo "GROUP stream=$key name=$name pending=${pending:-unknown}"
  done
done

printf '%s\n' '--- Redis client pressure ---'
redis CLIENT LIST 2>/dev/null | awk '
  BEGIN {FS=" "; print "client_id addr age idle db sub psub multi qbuf qbuf_free obl omem events cmd"}
  {
    id=addr=age=idle=db=sub=psub=multi=qbuf=qbuf_free=obl=omem=events=cmd="";
    for(i=1;i<=NF;i++){split($i,a,"="); if(a[1]=="id")id=a[2]; else if(a[1]=="addr")addr=a[2]; else if(a[1]=="age")age=a[2]; else if(a[1]=="idle")idle=a[2]; else if(a[1]=="db")db=a[2]; else if(a[1]=="sub")sub=a[2]; else if(a[1]=="psub")psub=a[2]; else if(a[1]=="multi")multi=a[2]; else if(a[1]=="qbuf")qbuf=a[2]; else if(a[1]=="qbuf-free")qbuf_free=a[2]; else if(a[1]=="obl")obl=a[2]; else if(a[1]=="omem")omem=a[2]; else if(a[1]=="events")events=a[2]; else if(a[1]=="cmd")cmd=a[2]}
    print id,addr,age,idle,db,sub,psub,multi,qbuf,qbuf_free,obl,omem,events,cmd
  }
' | sort -k13,13nr | head -n 300 || true

printf '%s\n' '--- Redis Archive container health/lifecycle ---'
if docker inspect "$ARCHIVE_CONTAINER" >/dev/null 2>&1; then
  docker inspect "$ARCHIVE_CONTAINER" --format 'RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}} OOMKilled={{.State.OOMKilled}} Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true
  docker stats --no-stream --format 'name={{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} mem_pct={{.MemPerc}} pids={{.PIDs}}' "$ARCHIVE_CONTAINER" 2>/dev/null || true
  echo 'Archive logs:'
  docker logs --since "${WINDOW_MINUTES}m" --timestamps "$ARCHIVE_CONTAINER" 2>&1 | tail -n 500 || true
else
  echo 'Archive container unavailable.'
fi

printf '%s\n' '--- Redis DLQ-retention container health/lifecycle ---'
if docker inspect "$DLQ_CONTAINER" >/dev/null 2>&1; then
  docker inspect "$DLQ_CONTAINER" --format 'RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}} OOMKilled={{.State.OOMKilled}} Status={{.State.Status}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true
  docker stats --no-stream --format 'name={{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} mem_pct={{.MemPerc}} pids={{.PIDs}}' "$DLQ_CONTAINER" 2>/dev/null || true
  echo 'DLQ-retention logs:'
  docker logs --since "${WINDOW_MINUTES}m" --timestamps "$DLQ_CONTAINER" 2>&1 | tail -n 300 || true
else
  echo 'DLQ-retention container unavailable.'
fi

printf '%s\n' '--- Safe conclusion hints (evidence only; no mutation) ---'
echo '1) Redis restart/loading evidence is separated from steady-state write latency.'
echo '2) Stream XLEN, group pending, consumer counts, client buffers, memory, CPU, persistence and slowlog are captured together.'
echo '3) Archive and DLQ-retention restart/health evidence is captured for timestamp correlation.'
echo '4) This snapshot performs no DEL, XTRIM, FLUSHDB, CONFIG SET, or restart operation.'
