#!/usr/bin/env bash
set -u

OUTPUT_DIR="${AITOS_STRATEGY_AUDIT_DIR:-$HOME/aitos-strategy-trade-lifecycle-audit}"
WINDOW_HOURS="${AITOS_STRATEGY_AUDIT_WINDOW_HOURS:-24}"
mkdir -p "$OUTPUT_DIR/raw"
REPORT="$OUTPUT_DIR/report.md"
RAW="$OUTPUT_DIR/raw"
STATUS=0

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

CH="aitos-clickhouse"
PAPER="aitos-paper"

if ! docker inspect "$CH" >/dev/null 2>&1; then
  echo "ClickHouse container unavailable." > "$REPORT"
  echo "BLOCKER: aitos-clickhouse not found" >> "$REPORT"
  exit 1
fi
if ! docker inspect "$PAPER" >/dev/null 2>&1; then
  echo "WARNING: aitos-paper container unavailable." > "$REPORT"
  STATUS=1
fi

ch() {
  docker exec "$CH" clickhouse-client --query "$1" 2>&1
}

latest_cte="(SELECT * FROM aitos.trades ORDER BY trade_id, recorded_at DESC, parseDateTimeBestEffortOrNull(exit_time) DESC, parseDateTimeBestEffortOrNull(entry_time) DESC LIMIT 1 BY trade_id)"

{
  echo '# AITOS Canonical Strategy & Trade Lifecycle Audit'
  echo
  echo "- Generated (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- Observation window for logs/recent performance: ${WINDOW_HOURS}h"
  echo '- Scope: scanner funnel → candidate → submission → open position → monitoring/exit decision → closed trade → PnL/R/MAE/MFE.'
  echo '- This is observational only. It does not modify trading state, position sizing, execution, or strategy logic.'
  echo

  echo '## 1. EXECUTIVE TRADE STATE'
  echo
  echo '### Latest lifecycle state per trade'
  ch "SELECT state, countDistinct(trade_id) AS trades FROM $latest_cte GROUP BY state ORDER BY state FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Currently open / exit-managed positions'
  ch "SELECT trade_id, symbol, side, strategy_id, state, entry_price, sl_price, tp_price, entry_time, exit_time, recorded_at AS updated_at, pnl, pnl_percent, risk_amount_usd, exit_reason FROM $latest_cte WHERE state IN ('position_opened','exit_triggered') ORDER BY parseDateTimeBestEffortOrNull(entry_time) ASC FORMAT TSVWithNames" > "$RAW/open_positions.tsv" || STATUS=1
  if [ -s "$RAW/open_positions.tsv" ]; then cat "$RAW/open_positions.tsv"; else echo 'No currently open positions found.'; fi
  echo

  echo '## 2. OPEN POSITION LIVE STATUS'
  echo
  echo 'Each open position is checked against the current Binance Futures ticker. PnL here is price-move based; it is not a replacement for persisted realized PnL.'
  echo
  if [ -s "$RAW/open_positions.tsv" ]; then
    tail -n +2 "$RAW/open_positions.tsv" | while IFS=$'\t' read -r trade_id symbol side strategy state entry_price sl_price tp_price entry_time exit_time recorded_at pnl pnl_percent risk_amount exit_reason; do
      [ -n "$symbol" ] || continue
      ticker="$(curl -fsS --max-time 5 "https://fapi.binance.com/fapi/v1/ticker/price?symbol=$symbol" 2>/dev/null || true)"
      current_price="$(printf '%s' "$ticker" | sed -n 's/.*"price":"\([0-9.eE+-]*\)".*/\1/p')"
      if [ -z "$current_price" ]; then
        echo "trade_id=$trade_id symbol=$symbol side=$side current_price=UNAVAILABLE market_status=UNKNOWN"
        continue
      fi
      python3 - "$trade_id" "$symbol" "$side" "$entry_price" "$sl_price" "$tp_price" "$entry_time" "$current_price" <<'PY'
import sys
from datetime import datetime, timezone

tid, symbol, side, entry, sl, tp, entry_time, current = sys.argv[1:]
try:
    entry=float(entry); sl=float(sl); tp=float(tp); current=float(current)
    move=((current-entry)/entry*100) if side.upper()=='LONG' else ((entry-current)/entry*100)
    to_sl=((current-sl)/current*100) if side.upper()=='LONG' else ((sl-current)/current*100)
    to_tp=((tp-current)/current*100) if side.upper()=='LONG' else ((current-tp)/current*100)
    e=datetime.fromisoformat(entry_time.replace('Z','+00:00'))
    age=max(0,(datetime.now(timezone.utc)-e).total_seconds()/3600)
    sl_state=('BREACHED' if ((side.upper()=='LONG' and current<=sl) or (side.upper()=='SHORT' and current>=sl)) else 'SAFE')
    tp_state=('REACHED' if ((side.upper()=='LONG' and current>=tp) or (side.upper()=='SHORT' and current<=tp)) else 'NOT_REACHED')
    print(f'trade_id={tid} symbol={symbol} side={side} entry={entry:.10g} current={current:.10g} move_pct={move:.4f} distance_to_sl_pct={to_sl:.4f} distance_to_tp_pct={to_tp:.4f} age_hours={age:.2f} sl={sl_state} tp={tp_state} state=OPEN')
except Exception as exc:
    print(f'trade_id={tid} symbol={symbol} state=OPEN calculation_error={exc}')
PY
    done
  else
    echo 'No open positions to evaluate.'
  fi
  echo

  echo '## 3. RECENT CLOSED-TRADE PERFORMANCE'
  echo
  echo '### All-time'
  ch "SELECT count() AS closed_trades, round(sum(ifNull(pnl,0)),4) AS net_pnl, round(avgIf(pnl,pnl IS NOT NULL),4) AS avg_pnl, round(sumIf(pnl,pnl>0),4) AS gross_profit, round(sumIf(pnl,pnl<0),4) AS gross_loss, round(100*countIf(pnl>0)/nullIf(countIf(pnl IS NOT NULL),0),2) AS win_rate_pct FROM $latest_cte WHERE state='position_closed' FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Recent window'
  ch "SELECT count() AS closed_trades, round(sum(ifNull(pnl,0)),4) AS net_pnl, round(avgIf(pnl,pnl IS NOT NULL),4) AS avg_pnl, round(sumIf(pnl,pnl>0),4) AS gross_profit, round(sumIf(pnl,pnl<0),4) AS gross_loss, round(100*countIf(pnl>0)/nullIf(countIf(pnl IS NOT NULL),0),2) AS win_rate_pct FROM $latest_cte WHERE state='position_closed' AND parseDateTimeBestEffortOrNull(exit_time)>=now()-INTERVAL ${WINDOW_HOURS} HOUR FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Exit reason breakdown'
  ch "SELECT ifNull(exit_reason,'UNKNOWN') AS exit_reason, count() AS trades, round(sum(ifNull(pnl,0)),4) AS net_pnl, round(avgIf(pnl,pnl IS NOT NULL),4) AS avg_pnl, round(100*countIf(pnl>0)/nullIf(countIf(pnl IS NOT NULL),0),2) AS win_rate_pct FROM $latest_cte WHERE state='position_closed' GROUP BY exit_reason ORDER BY trades DESC FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Strategy performance'
  ch "SELECT strategy_id, count() AS trades, round(sum(ifNull(pnl,0)),4) AS net_pnl, round(avgIf(pnl,pnl IS NOT NULL),4) AS avg_pnl, round(100*countIf(pnl>0)/nullIf(countIf(pnl IS NOT NULL),0),2) AS win_rate_pct FROM $latest_cte WHERE state='position_closed' GROUP BY strategy_id ORDER BY net_pnl DESC FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Symbol / side performance'
  ch "SELECT symbol, side, count() AS trades, round(sum(ifNull(pnl,0)),4) AS net_pnl, round(avgIf(pnl,pnl IS NOT NULL),4) AS avg_pnl, round(100*countIf(pnl>0)/nullIf(countIf(pnl IS NOT NULL),0),2) AS win_rate_pct FROM $latest_cte WHERE state='position_closed' GROUP BY symbol, side ORDER BY net_pnl DESC FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Last 20 closed trades'
  ch "SELECT trade_id, symbol, side, strategy_id, entry_price, exit_price, sl_price, tp_price, exit_reason, round(pnl,4) AS pnl, round(pnl_percent,4) AS pnl_percent, entry_time, exit_time FROM $latest_cte WHERE state='position_closed' ORDER BY parseDateTimeBestEffortOrNull(exit_time) DESC LIMIT 20 FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo

  echo '## 4. RISK / R-MULTIPLE'
  echo
  ch "SELECT exit_reason, count() AS trades, round(avgIf(pnl/risk_amount_usd,risk_amount_usd>0 AND pnl IS NOT NULL),3) AS avg_R, round(sumIf(pnl/risk_amount_usd,risk_amount_usd>0 AND pnl IS NOT NULL),3) AS total_R FROM $latest_cte WHERE state='position_closed' GROUP BY exit_reason ORDER BY trades DESC FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo 'Worst 10 closed trades:'
  ch "SELECT trade_id, symbol, side, strategy_id, exit_reason, round(pnl,4) AS pnl, round(pnl/risk_amount_usd,3) AS R, entry_price, exit_price, sl_price, tp_price FROM $latest_cte WHERE state='position_closed' AND pnl IS NOT NULL ORDER BY pnl ASC LIMIT 10 FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo

  echo '## 5. EXCURSION / EXIT QUALITY'
  echo
  ch "SELECT count() AS closed_trades, countIf(mae_price IS NOT NULL) AS trades_with_mae, countIf(mfe_price IS NOT NULL) AS trades_with_mfe, round(100*countIf(mae_price IS NOT NULL)/count(),2) AS mae_coverage_pct, round(100*countIf(mfe_price IS NOT NULL)/count(),2) AS mfe_coverage_pct, round(avgIf(mae_r,mae_r IS NOT NULL),4) AS avg_mae_R, round(avgIf(mfe_r,mfe_r IS NOT NULL),4) AS avg_mfe_R, round(quantileExactIf(0.5)(mae_r,mae_r IS NOT NULL),4) AS median_mae_R, round(quantileExactIf(0.5)(mfe_r,mfe_r IS NOT NULL),4) AS median_mfe_R FROM $latest_cte WHERE state='position_closed' AND parseDateTimeBestEffortOrNull(exit_time)>=now()-INTERVAL ${WINDOW_HOURS} HOUR FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Strategy-wise MAE/MFE'
  ch "SELECT strategy_id, count() AS trades, countIf(mae_r IS NOT NULL) AS mae_samples, countIf(mfe_r IS NOT NULL) AS mfe_samples, round(100*countIf(mae_r IS NOT NULL)/count(),2) AS mae_coverage_pct, round(100*countIf(mfe_r IS NOT NULL)/count(),2) AS mfe_coverage_pct, round(avgIf(mae_r,mae_r IS NOT NULL),4) AS avg_mae_R, round(avgIf(mfe_r,mfe_r IS NOT NULL),4) AS avg_mfe_R FROM $latest_cte WHERE state='position_closed' AND parseDateTimeBestEffortOrNull(exit_time)>=now()-INTERVAL ${WINDOW_HOURS} HOUR GROUP BY strategy_id ORDER BY trades DESC FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo
  echo '### Last 20 closed trades with excursion telemetry'
  ch "SELECT trade_id, symbol, side, strategy_id, round(mae_r,4) AS mae_R, round(mfe_r,4) AS mfe_R, round(pnl,4) AS pnl, exit_reason, exit_time FROM $latest_cte WHERE state='position_closed' AND parseDateTimeBestEffortOrNull(exit_time)>=now()-INTERVAL ${WINDOW_HOURS} HOUR ORDER BY parseDateTimeBestEffortOrNull(exit_time) DESC LIMIT 20 FORMAT PrettyCompactMonoBlock" || STATUS=1
  echo

  echo '## 6. STRATEGY FUNNEL — LOG EVIDENCE'
  echo
  if docker inspect "$PAPER" >/dev/null 2>&1; then
    docker logs --since "${WINDOW_HOURS}h" --timestamps "$PAPER" > "$RAW/paper_logs.txt" 2>&1 || true
    grep -E 'paper signal diagnostics|scanner score breakdown|scanner ranking decision|trade candidate|candidate.*(accepted|rejected|skipped)|kernel.*(accepted|rejected)|trade.*submitted' "$RAW/paper_logs.txt" > "$RAW/funnel_events.txt" || true
    echo '### Funnel event counts'
    printf '  scanner diagnostics: '; grep -c 'paper signal diagnostics' "$RAW/funnel_events.txt" 2>/dev/null || true
    printf '  score breakdowns: '; grep -c 'scanner score breakdown' "$RAW/funnel_events.txt" 2>/dev/null || true
    printf '  ranking decisions: '; grep -c 'scanner ranking decision' "$RAW/funnel_events.txt" 2>/dev/null || true
    printf '  candidate/kernel rejections: '; grep -c -E 'candidate.*rejected|kernel.*rejected' "$RAW/funnel_events.txt" 2>/dev/null || true
    printf '  candidate accepted/submitted: '; grep -c -E 'candidate.*accepted|candidate.*submitted|trade.*submitted' "$RAW/funnel_events.txt" 2>/dev/null || true
    echo
    echo '### Recent funnel events (last 300)'
    tail -n 300 "$RAW/funnel_events.txt" || true
  else
    echo 'aitos-paper unavailable; scanner log evidence incomplete.'
  fi
  echo

  echo '## 7. TRADE LIFECYCLE + EXIT INTELLIGENCE'
  echo
  if [ -s "$RAW/paper_logs.txt" ]; then
    grep -Ei 'trade.*(submitted|opened|closed|exit|stop.?loss|take.?profit|trailing)|position.*(opened|closed|exit)|exit intelligence|position.?manager|market.?context|hold|thesis|invalidation|exit decision|exit score|monitor_tier|priority_score' "$RAW/paper_logs.txt" > "$RAW/lifecycle_events.txt" || true
    echo '### Lifecycle/Exit events (last 500)'
    tail -n 500 "$RAW/lifecycle_events.txt" || true
    echo
    echo '### Per-open-position symbol context'
    if [ -s "$RAW/open_positions.tsv" ]; then
      cut -f2 "$RAW/open_positions.tsv" | tail -n +2 | sort -u | while read -r symbol; do
        [ -n "$symbol" ] || continue
        echo "#### $symbol"
        grep -F "$symbol" "$RAW/lifecycle_events.txt" | tail -n 80 || echo 'No matching lifecycle context.'
      done
    else
      echo 'No open positions.'
    fi
  else
    echo 'No application logs available.'
  fi
  echo

  echo '## 8. POSITION MONITOR / DEEP-ANALYSIS CONTEXT'
  echo
  if [ -s "$RAW/paper_logs.txt" ]; then
    grep -Ei 'POSITION_MONITOR|monitor_tier|EXIT_CANDIDATE|position.?monitor|deep.?priority|priority_score|PositionHealthVector|protected.*position|position universe|MAX_DEEP_SYMBOLS' "$RAW/paper_logs.txt" | tail -n 500 || echo 'No explicit position-monitor telemetry found in the selected log window.'
  else
    echo 'No application logs available.'
  fi
  echo

  echo '## 9. DATA / EXECUTION QUALITY FLAGS'
  echo
  if [ -s "$RAW/paper_logs.txt" ]; then
    echo 'Redis connection pressure:'
    grep -c 'Too many connections' "$RAW/paper_logs.txt" 2>/dev/null || true
    echo 'Market-data reconnects:'
    grep -c -Ei 'canonical_reconnect_scheduled|reconnect(ing|ed)?' "$RAW/paper_logs.txt" 2>/dev/null || true
    echo 'Order-book sequence/bootstrap errors:'
    grep -c -Ei 'order.?book.*sequence|sequence.*(break|error)|bootstrap.*(mismatch|error)|OrderBookSequenceError' "$RAW/paper_logs.txt" 2>/dev/null || true
    echo 'Stale/freshness drops:'
    grep -c -Ei 'freshness.*drop|stale.*market|queued market event age' "$RAW/paper_logs.txt" 2>/dev/null || true
    echo 'Execution/order errors:'
    grep -c -Ei 'order.*(rejected|failed)|execution.*error|exchange.*error' "$RAW/paper_logs.txt" 2>/dev/null || true
  else
    echo 'No application logs available.'
  fi
  echo

  echo '## 10. AUDIT INTERPRETATION RULES'
  echo
  echo '- Scanner activity is not an entry. Candidate/submission/opened/closed are separate lifecycle gates.'
  echo '- Open-position truth comes from the latest lifecycle snapshot per trade_id, not raw historical rows.'
  echo '- Current-price status is a live market check and must not be confused with realized PnL.'
  echo '- A healthy strategy audit requires agreement between persisted lifecycle state and application lifecycle/exit telemetry.'
  echo '- An open position with no recent symbol-specific monitoring/exit evidence is a telemetry or lifecycle-coverage finding, not proof that the strategy is wrong.'
  echo '- Exit quality should be judged using exit reason, R, MAE/MFE and subsequent market context where available—not PnL alone.'
  echo '- Data-path failures such as Redis pressure, stale data or order-book sequence errors are reported as confounders and are not silently attributed to strategy logic.'
  echo
  echo '## 11. RAW EVIDENCE'
  echo
  echo '- raw/open_positions.tsv — latest open-position inventory.'
  echo '- raw/paper_logs.txt — selected observation-window application logs.'
  echo '- raw/funnel_events.txt — scanner/candidate funnel events.'
  echo '- raw/lifecycle_events.txt — lifecycle and exit-intelligence events.'
  echo
  echo "Audit collector status: $STATUS"
} > "$REPORT" 2>&1

cat "$REPORT"
printf '%s\n' "$STATUS" > "$OUTPUT_DIR/exit_code"
exit "$STATUS"
