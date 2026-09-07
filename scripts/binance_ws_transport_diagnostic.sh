#!/usr/bin/env bash
set -u

# Transport-only probe. It deliberately does not modify AITOS state.
# Run from the VPS host; it also probes from inside aitos-paper.
ENDPOINT="${AITOS_BINANCE_WS_DIAG_ENDPOINT:-wss://fstream.binance.com/stream?streams=btcusdt@aggTrade}"
HOST="fstream.binance.com"
PORT="443"
CONTAINER="${AITOS_PAPER_CONTAINER:-aitos-paper}"
TIMEOUT="${AITOS_WS_DIAG_TIMEOUT_SECONDS:-20}"
OUT="${AITOS_WS_DIAG_OUTPUT:-/tmp/aitos-binance-ws-transport-diagnostic}"
mkdir -p "$OUT"
: > "$OUT/report.md"

section() { printf '\n## %s\n\n' "$1" | tee -a "$OUT/report.md"; }
run() { local name="$1"; shift; printf '\n### %s\n\n```text\n' "$name" | tee -a "$OUT/report.md"; timeout "$TIMEOUT" bash -lc "$*" 2>&1 | tee -a "$OUT/report.md" || true; printf '\n```\n' | tee -a "$OUT/report.md"; }

printf '# Binance WebSocket Transport Diagnostic\n\n- UTC: %s\n- Endpoint: `%s`\n- Host: `%s:%s`\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ENDPOINT" "$HOST" "$PORT" > "$OUT/report.md"

section "Host DNS"
run "getent" "getent ahosts $HOST"
run "dig" "command -v dig >/dev/null && dig +time=3 +tries=1 $HOST || true"

section "Host TCP"
run "TCP 443" "if command -v nc >/dev/null; then nc -vz -w 5 $HOST $PORT; else timeout 5 bash -c '</dev/tcp/$HOST/$PORT' && echo TCP_OK || echo TCP_FAIL; fi"

section "Host TLS"
run "TLS handshake" "openssl s_client -connect ${HOST}:${PORT} -servername ${HOST} -brief </dev/null"

section "Host HTTP/1.1 upgrade reachability"
run "HTTP upgrade" "curl --http1.1 -sS -i --max-time 10 -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: SGVsbG9BdG9zV1M=' 'https://${HOST}/stream?streams=btcusdt@aggTrade'"

section "Container identity/network"
run "container" "docker inspect -f '{{.Config.Image}} {{.State.Status}}' '$CONTAINER'"
run "container DNS" "docker exec '$CONTAINER' getent ahosts '$HOST'"
run "container TCP" "docker exec '$CONTAINER' sh -lc 'if command -v nc >/dev/null; then nc -vz -w 5 '$HOST' '$PORT'; else wget -q --spider --timeout=5 https://'$HOST'/ || true; fi'"
run "container TLS" "docker exec '$CONTAINER' sh -lc 'command -v openssl >/dev/null && openssl s_client -connect '$HOST':'$PORT' -servername '$HOST' -brief </dev/null || true'"

section "Container WebSocket first-message probe"
docker exec -i "$CONTAINER" env AITOS_DIAG_ENDPOINT="$ENDPOINT" AITOS_DIAG_TIMEOUT="$TIMEOUT" python3 - <<'PY' | tee -a "$OUT/report.md" || true
import asyncio, json, os, socket, ssl, time

URL=os.environ["AITOS_DIAG_ENDPOINT"]
TIMEOUT=float(os.environ["AITOS_DIAG_TIMEOUT"])

async def main():
    try:
        import websockets
    except Exception as exc:
        print(json.dumps({"stage":"import_websockets","ok":False,"error":repr(exc)}))
        return
    t0=time.monotonic()
    try:
        async with asyncio.timeout(TIMEOUT):
            async with websockets.connect(URL, ping_interval=15, ping_timeout=10, open_timeout=10, close_timeout=5) as ws:
                connected=time.monotonic()
                print(json.dumps({"stage":"websocket_handshake","ok":True,"connect_ms":round((connected-t0)*1000,1)}))
                try:
                    raw=await asyncio.wait_for(ws.recv(), timeout=min(10,TIMEOUT))
                    first=time.monotonic()
                    try:
                        obj=json.loads(raw)
                        summary={"stream":obj.get("stream"),"event":(obj.get("data") or {}).get("e"),"symbol":(obj.get("data") or {}).get("s"),"event_time":(obj.get("data") or {}).get("E")}
                    except Exception:
                        summary={"raw_prefix":str(raw)[:300]}
                    print(json.dumps({"stage":"first_message","ok":True,"first_message_ms":round((first-t0)*1000,1),**summary}))
                    pong_t=time.monotonic()
                    pong_wait=await ws.ping()
                    await asyncio.wait_for(pong_wait, timeout=10)
                    print(json.dumps({"stage":"ping_pong","ok":True,"ping_pong_ms":round((time.monotonic()-pong_t)*1000,1)}))
                except Exception as exc:
                    print(json.dumps({"stage":"message_or_ping","ok":False,"error_type":type(exc).__name__,"error":str(exc),"close_code":ws.close_code,"close_reason":ws.close_reason}))
    except Exception as exc:
        print(json.dumps({"stage":"websocket_handshake","ok":False,"error_type":type(exc).__name__,"error":str(exc)}))

asyncio.run(main())
PY

section "Verdict"
python3 - "$OUT/report.md" <<'PY'
import re,sys
p=sys.argv[1]
s=open(p,encoding='utf-8').read()
checks={
 'host_dns': bool(re.search(r'getent.*?\n.*?fstream',s,re.S)),
 'host_tcp': 'TCP_FAIL' not in s,
 'ws_first_message': '"stage": "first_message", "ok": true' in s or '"stage":"first_message","ok":true' in s,
}
print('Host DNS:', 'PASS' if checks['host_dns'] else 'UNKNOWN')
print('Host TCP/TLS:', 'PASS' if checks['host_tcp'] else 'FAIL')
print('WebSocket first message:', 'PASS' if checks['ws_first_message'] else 'FAIL/NO-DATA')
print('\nInterpretation:')
if checks['ws_first_message']:
    print('Binance WebSocket transport is reachable from inside aitos-paper; investigate AITOS runtime/subscription/consumer path.')
else:
    print('No first aggTrade message was received by the container probe; compare DNS/TCP/TLS/HTTP stages above before changing parser or Redis code.')
PY

printf '\nReport: %s\n' "$OUT/report.md"