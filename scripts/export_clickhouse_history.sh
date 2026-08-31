#!/usr/bin/env bash
set -euo pipefail

# Export bounded, replay-friendly market history from the local AITOS ClickHouse container.
# Usage: scripts/export_clickhouse_history.sh SYMBOL HOURS OUTPUT_DIR

SYMBOL="${1:?symbol required}"
HOURS="${2:?hours required}"
OUTPUT_DIR="${3:-./aitos-history-export}"

case "$SYMBOL" in
  (*[!A-Za-z0-9._-]*) echo "Invalid symbol" >&2; exit 2 ;;
esac
case "$HOURS" in
  (*[!0-9]*) echo "Invalid hours" >&2; exit 2 ;;
esac
(( HOURS >= 1 && HOURS <= 720 )) || { echo "hours must be 1..720" >&2; exit 2; }

mkdir -p "$OUTPUT_DIR/data"
START="$(date -u -d "-${HOURS} hours" '+%Y-%m-%d %H:%M:%S')"
END="$(date -u '+%Y-%m-%d %H:%M:%S')"

cat > "$OUTPUT_DIR/manifest.json" <<EOF
{
  "symbol": "${SYMBOL}",
  "start_utc": "${START}",
  "end_utc": "${END}",
  "hours": ${HOURS},
  "source": "aitos-clickhouse",
  "format": "JSONEachRow.gz"
}
EOF

docker inspect aitos-clickhouse >/dev/null

docker exec aitos-clickhouse clickhouse-client --query \
  "SELECT time, price, quantity, side, trade_id, is_buyer_maker FROM aitos.trade_ticks WHERE symbol = '${SYMBOL}' AND time >= toDateTime64('${START}', 3) AND time < toDateTime64('${END}', 3) ORDER BY time FORMAT JSONEachRow" \
  | gzip -1 > "$OUTPUT_DIR/data/trade_ticks.jsonl.gz"

docker exec aitos-clickhouse clickhouse-client --query \
  "SELECT time, bid_levels, ask_levels, spread, depth_ratio, last_update_id FROM aitos.order_book_snapshots WHERE symbol = '${SYMBOL}' AND time >= toDateTime64('${START}', 3) AND time < toDateTime64('${END}', 3) ORDER BY time FORMAT JSONEachRow" \
  | gzip -1 > "$OUTPUT_DIR/data/order_book_snapshots.jsonl.gz"

echo "Exported $SYMBOL for ${HOURS}h: $OUTPUT_DIR"
