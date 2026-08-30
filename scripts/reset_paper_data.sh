#!/usr/bin/env bash
# Reset AITOS paper-trading state/performance data.
# Market-history tables are intentionally preserved for future backtests.
set -euo pipefail

CONFIRMATION="${1:-}"
if [[ "$CONFIRMATION" != "RESET_PAPER_DATA" ]]; then
  echo "Refusing reset: pass RESET_PAPER_DATA as the first argument." >&2
  exit 2
fi

if ! docker inspect aitos-clickhouse >/dev/null 2>&1; then
  echo "aitos-clickhouse container not found" >&2
  exit 1
fi

exec 9>"$HOME/.aitos-paper-reset.lock"
flock 9

echo "=== Stopping paper/learning consumers ==="
docker stop aitos-paper aitos-learning >/dev/null 2>&1 || true

CH=(docker exec aitos-clickhouse clickhouse-client --database aitos)

# These are paper-trading state/performance stores, not market-history stores.
for table in trades journal_entries trade_runtime_state portfolio_drawdown_state learning_experiences; do
  echo "TRUNCATE TABLE $table"
  "${CH[@]}" --query="TRUNCATE TABLE IF EXISTS $table"
done

# Remove old lifecycle/decision events so durable consumers cannot replay the
# pre-reset paper-trading epoch into the newly empty state tables.
if docker inspect aitos-redis >/dev/null 2>&1; then
  echo "=== Removing stale paper lifecycle streams from Redis ==="
  docker exec aitos-redis sh -lc '
    set -eu
    for pattern in "stream:trade.*" "stream:decision.*" "stream:dlq"; do
      redis-cli --scan --pattern "$pattern" | while IFS= read -r key; do
        [ -z "$key" ] || redis-cli DEL "$key" >/dev/null
      done
    done
  '
fi

echo "=== Starting paper/learning consumers ==="
docker start aitos-learning aitos-paper >/dev/null

sleep 5

echo "=== Post-reset verification ==="
for table in trades journal_entries trade_runtime_state portfolio_drawdown_state learning_experiences; do
  count=$("${CH[@]}" --query="SELECT count() FROM $table")
  echo "$table rows: $count"
done

echo "Paper data reset completed. Market-history tables were preserved."
