#!/usr/bin/env bash
# Real-time paper-trading smoke test for Codespaces / local Docker.
# Observational: starts infra, runs paper trader briefly, probes /health.
set -euo pipefail
cd "$(dirname "$0")/.."

export AITOS_MODE=paper
export BINANCE_TESTNET="${BINANCE_TESTNET:-true}"
export ENVIRONMENT="${ENVIRONMENT:-dev}"

if [[ ! -f .env ]]; then
  bash scripts/codespace_bootstrap.sh
fi

echo "[smoke] starting infra (redis + clickhouse + neo4j)"
docker compose up -d redis clickhouse clickhouse-init neo4j
echo "[smoke] waiting for redis/clickhouse health..."
for i in $(seq 1 60); do
  if docker compose exec -T redis redis-cli --no-auth-warning -a "${REDIS_PASSWORD:-local-development-only-change-this}" ping 2>/dev/null | grep -q PONG \
     && docker compose exec -T clickhouse clickhouse-client --query 'SELECT 1' >/dev/null 2>&1; then
    echo "[smoke] infra healthy after ${i}s"
    break
  fi
  sleep 2
  if [[ $i -eq 60 ]]; then
    echo "[smoke] infra not healthy in time" >&2
    docker compose ps
    exit 1
  fi
done

echo "[smoke] Binance connectivity probe (testnet)"
python3 - <<'PY'
import urllib.request, ssl, json
ctx = ssl.create_default_context()
url = "https://testnet.binancefuture.com/fapi/v1/ticker/price?symbol=BTCUSDT"
with urllib.request.urlopen(url, context=ctx, timeout=15) as r:
    data = json.loads(r.read())
    print("BTCUSDT testnet price:", data.get("price"))
PY

echo "[smoke] starting paper trading in background (90s window)"
mkdir -p data/runtime/logs
python3 run_paper_trading.py > data/runtime/logs/paper_smoke.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

OK=0
for i in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:8090/health >/tmp/aitos_health.json 2>/dev/null; then
    echo "[smoke] /health OK at ${i}*2s"
    cat /tmp/aitos_health.json | head -c 800; echo
    OK=1
    break
  fi
  if ! kill -0 $PID 2>/dev/null; then
    echo "[smoke] paper process exited early" >&2
    tail -n 80 data/runtime/logs/paper_smoke.log || true
    exit 1
  fi
  sleep 2
done

if [[ $OK -ne 1 ]]; then
  echo "[smoke] /health never became ready" >&2
  tail -n 120 data/runtime/logs/paper_smoke.log || true
  exit 1
fi

echo "[smoke] probing /metrics and /data-flow (if present)"
curl -fsS http://127.0.0.1:8090/metrics 2>/dev/null | head -n 30 || true
curl -fsS http://127.0.0.1:8090/data-flow 2>/dev/null | head -c 600 || true
echo
echo "[smoke] recent paper log:"
tail -n 40 data/runtime/logs/paper_smoke.log || true
echo "[smoke] SUCCESS — paper trader responded on :8090 with live testnet data path"
