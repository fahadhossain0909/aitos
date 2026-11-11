"""Read-only, reproducible analytics over a bounded CLOSED-trade ledger sample.

Not account equity, mark-to-market drawdown, a Sharpe estimate or promotion logic.
Recorded net PnL is used as-is: costs are never subtracted twice. Missing fields
stay unavailable; malformed records invalidate the report rather than vanish.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from numbers import Real

FIELDS = (
    "trade_id",
    "symbol",
    "side",
    "state",
    "pnl",
    "entry_time",
    "exit_time",
    "fees_paid_usd",
    "funding_pnl_usd",
    "strategy_id",
)


def _number(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} requires finite numeric evidence")
    return float(value)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("closed ledger requires explicit timestamps")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except ValueError:
        raise ValueError("closed ledger requires timezone-aware timestamps") from None


def _summary(rows):
    pnls = [r["pnl"] for r in rows]
    wins = sum(p > 0 for p in pnls)
    losses = sum(p < 0 for p in pnls)
    positive = math.fsum(p for p in pnls if p > 0)
    negative = -math.fsum(p for p in pnls if p < 0)
    return dict(
        trades=len(rows),
        wins=wins,
        losses=losses,
        breakeven=len(rows) - wins - losses,
        net_pnl_usd=math.fsum(pnls) if pnls else None,
        win_rate=wins / len(rows) if rows else None,
        expectancy_usd=statistics.fmean(pnls) if pnls else None,
        median_pnl_usd=statistics.median(pnls) if pnls else None,
        profit_factor=positive / negative if negative > 0 else None,
        profit_factor_reason=(
            None if negative > 0 else "No observed losses; ratio is undefined."
        ),
        best_trade_usd=max(pnls) if pnls else None,
        worst_trade_usd=min(pnls) if pnls else None,
    )


def _add_partial(partials: list[float], value: float) -> None:
    """Error-free float expansion for stable prefixes without O(n²) resums.

    Preserve low-order components across large cancelling closes. Drawdown is
    still observed only AFTER all trades sharing a close timestamp are added.
    """
    index = 0
    for previous in partials:
        if abs(value) < abs(previous):
            value, previous = previous, value
        high = value + previous
        if not math.isfinite(high):
            raise ValueError("performance accumulation overflow")
        low = previous - (high - value)
        if low:
            partials[index] = low
            index += 1
        value = high
    partials[index:] = [value]


def analyze_closed_trades(
    rows: list[dict], *, window_limit: int = 1000, truncated: bool = False
) -> dict:
    if (
        isinstance(window_limit, bool)
        or not isinstance(window_limit, int)
        or not 1 <= window_limit <= 10000
        or len(rows) > window_limit
    ):
        raise ValueError("bounded performance window required")
    normalized = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("state") != "position_closed":
            raise ValueError("performance requires closed ledger records")
        item = {k: row[k] for k in FIELDS if k in row}
        for key in ("trade_id", "symbol", "side"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError("closed ledger identity required")
        if item["trade_id"] in ids or item["side"] not in {"LONG", "SHORT"}:
            raise ValueError("duplicate trade or invalid direction")
        ids.add(item["trade_id"])
        item["pnl"] = _number(item.get("pnl"), "net PnL")
        start, end = _timestamp(item.get("entry_time")), _timestamp(
            item.get("exit_time")
        )
        if end < start:
            raise ValueError("close timestamp precedes entry")
        item["entry_time"], item["exit_time"] = start.isoformat(), end.isoformat()
        if item.get("strategy_id") is not None and (
            not isinstance(item["strategy_id"], str) or not item["strategy_id"]
        ):
            raise ValueError("invalid strategy identity")
        for key in ("fees_paid_usd", "funding_pnl_usd"):
            if item.get(key) is not None:
                item[key] = _number(item[key], key)
        normalized.append(item)
    normalized.sort(key=lambda r: (r["exit_time"], r["trade_id"]))
    digest = hashlib.sha256(
        json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    grouped = defaultdict(list)
    for row in normalized:
        grouped[row["exit_time"]].append(row["pnl"])
    cumulative, peak, max_drawdown = 0.0, 0.0, 0.0
    curve = []
    partials: list[float] = []
    for timestamp, values in sorted(grouped.items()):
        for value in values:
            _add_partial(partials, value)
        cumulative = math.fsum(partials)
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_drawdown = max(max_drawdown, drawdown)
        curve.append(dict(at=timestamp, net_pnl_usd=cumulative, drawdown_usd=drawdown))
    costs = {}
    for key in ("fees_paid_usd", "funding_pnl_usd"):
        available = [r[key] for r in normalized if r.get(key) is not None]
        costs[key] = dict(
            observed_rows=len(available),
            total_rows=len(rows),
            total_usd=(
                math.fsum(available) if rows and len(available) == len(rows) else None
            ),
        )
    summary = _summary(normalized)
    summary["max_closed_pnl_drawdown_usd"] = max_drawdown if rows else None
    direction_rows = defaultdict(list)
    strategy_rows = defaultdict(list)
    for row in normalized:
        direction_rows[row["side"]].append(row)
        strategy_rows[row.get("strategy_id") or "unclassified"].append(row)
    report = dict(
        schema_version=1,
        available=True,
        source="clickhouse_closed_ledger",
        scope="bounded_recorded_net_pnl_not_account_equity",
        selection="latest_ledger_updates",
        evidence_sha256=digest,
        window_limit=window_limit,
        loaded_trades=len(rows),
        truncated=bool(truncated),
        start=normalized[0]["exit_time"] if rows else None,
        end=normalized[-1]["exit_time"] if rows else None,
        summary=summary,
        curve=curve,
        costs=costs,
        directions={side: _summary(direction_rows[side]) for side in ("LONG", "SHORT")},
        strategies={key: _summary(strategy_rows[key]) for key in sorted(strategy_rows)},
        limitations=[
            "Bounded closed-ledger sample, not all-time account performance.",
            "Recorded PnL may include prior partial reductions; curve is allocated at final close, not cash-flow timing.",
            "No deposits, withdrawals, open exposure or mark-to-market valuation is included.",
            "Equal close timestamps are aggregated before drawdown; no artificial intra-timestamp path is inferred.",
            "No Sharpe, annualized return, significance or live-readiness claim is inferred.",
        ],
    )

    def finite_derived(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite derived performance evidence")
        if isinstance(value, dict):
            for item in value.values():
                finite_derived(item)
        elif isinstance(value, list):
            for item in value:
                finite_derived(item)

    finite_derived(report)
    return report
