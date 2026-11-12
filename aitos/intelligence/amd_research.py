"""Point-in-time AMD research, event-flow and execution-aware validation helpers.

These helpers produce research labels and replay measurements only. They do not
create trading signals, infer hidden queue position, or promote AMD into gates.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable, Mapping, Sequence

from aitos.intelligence.liquidity_intelligence import OrderFlowEvidence
from aitos.statistics.replay import (
    BookSnapshot,
    ExecutionReplayResult,
    replay_historical_order,
)


@dataclass(frozen=True)
class AMDTradePrint:
    timestamp_ms: int
    trade_id: str
    price: float
    quantity: float
    aggressor_side: str | None


@dataclass(frozen=True)
class AMDDepthSnapshot:
    timestamp_ms: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class EventFlowResult:
    evidence: OrderFlowEvidence
    trade_count: int
    depth_update_count: int
    duplicate_trade_count: int
    unclassified_trade_count: int
    latest_event_ms: int | None
    event_window_start_ms: int
    diagnostics: tuple[str, ...]


def _validate_timestamp(value: int) -> int:
    result = int(value)
    if result < 0:
        raise ValueError("event timestamps must be non-negative")
    return result


def _best_level(levels: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    if not levels:
        return None
    price, size = levels[0]
    price, size = float(price), float(size)
    if not isfinite(price) or not isfinite(size) or price <= 0 or size < 0:
        raise ValueError(
            "depth levels must contain finite positive prices and non-negative size"
        )
    return price, size


def _book_ofi(
    previous: AMDDepthSnapshot, current: AMDDepthSnapshot
) -> tuple[float, float, float]:
    prev_bid = _best_level(previous.bids)
    curr_bid = _best_level(current.bids)
    prev_ask = _best_level(previous.asks)
    curr_ask = _best_level(current.asks)
    if not all((prev_bid, curr_bid, prev_ask, curr_ask)):
        return 0.0, 0.0, 0.0

    pbp, pbs = prev_bid
    cbp, cbs = curr_bid
    pap, pas = prev_ask
    cap, cas = curr_ask

    bid_delta = cbs if cbp > pbp else cbs - pbs if cbp == pbp else -pbs
    ask_delta = -cas if cap < pap else pas - cas if cap == pap else pas
    bid_add = max(0.0, cbs - pbs) if cbp == pbp else 0.0
    ask_add = max(0.0, cas - pas) if cap == pap else 0.0
    return bid_delta + ask_delta, bid_add, ask_add


def _event_time_ms(value: Any) -> int:
    if hasattr(value, "timestamp"):
        return _validate_timestamp(round(value.timestamp() * 1000))
    return _validate_timestamp(value)


def build_live_event_order_flow(
    trade_ticks: Iterable[Any],
    depth_events: Iterable[Mapping[str, Any]],
    *,
    as_of_ms: int,
    window_ms: int = 60_000,
    max_staleness_ms: int = 5_000,
    min_trade_events: int = 10,
    min_depth_events: int = 2,
    history_gap_at_ms: int | None = None,
) -> EventFlowResult:
    """Consume AITOS TradeTick and sequenced LocalOrderBook depth-event records."""
    as_of = _validate_timestamp(as_of_ms)
    if window_ms <= 0 or max_staleness_ms < 0:
        raise ValueError("window_ms must be positive and staleness non-negative")
    start = max(0, as_of - int(window_ms))
    diagnostics: list[str] = []
    trades_by_id: dict[str, Any] = {}
    duplicates = 0
    unclassified = 0
    for trade in trade_ticks:
        timestamp = _event_time_ms(getattr(trade, "timestamp", None))
        if timestamp < start or timestamp > as_of:
            continue
        trade_id = str(getattr(trade, "trade_id", ""))
        if not trade_id:
            unclassified += 1
            continue
        if trade_id in trades_by_id:
            duplicates += 1
            continue
        side = getattr(trade, "side", None)
        side = str(getattr(side, "value", side) or "").upper()
        if side not in {"BUY", "SELL"}:
            unclassified += 1
            continue
        price = float(getattr(trade, "price", 0.0))
        quantity = float(getattr(trade, "quantity", 0.0))
        if not isfinite(price) or not isfinite(quantity) or price <= 0 or quantity <= 0:
            raise ValueError("live trade values must be finite and positive")
        trades_by_id[trade_id] = (timestamp, side, price, quantity)

    trades = sorted(trades_by_id.values(), key=lambda item: item[0])
    total_notional = sum(price * quantity for _, _, price, quantity in trades)
    signed_notional = sum(
        (1.0 if side == "BUY" else -1.0) * price * quantity
        for _, side, price, quantity in trades
    )
    cvd_ratio = signed_notional / total_notional if total_notional else 0.0
    cvd_score = max(0.0, min(10.0, 5.0 + 5.0 * cvd_ratio))

    events = []
    for event in depth_events:
        timestamp = _validate_timestamp(int(event.get("event_time_ms", 0)))
        if start <= timestamp <= as_of:
            events.append((timestamp, event))
    events.sort(key=lambda item: item[0])
    sequence_valid = True
    if history_gap_at_ms is not None:
        gap_timestamp = _validate_timestamp(history_gap_at_ms)
        if gap_timestamp >= start:
            sequence_valid = False
            diagnostics.append("depth_history_gap_in_window")
    if any(bool(event.get("sequence_gap", False)) for _, event in events):
        sequence_valid = False
        diagnostics.append("depth_sequence_gap_marker")
    ofi_raw = 0.0
    ofi_scale = 0.0
    bid_replenishment = 0.0
    ask_replenishment = 0.0
    for index, (_, event) in enumerate(events):
        raw = float(event.get("ofi_raw", 0.0))
        scale = float(event.get("ofi_scale", 0.0))
        bid_add = float(event.get("bid_replenishment_qty", 0.0))
        ask_add = float(event.get("ask_replenishment_qty", 0.0))
        if any(not isfinite(value) for value in (raw, scale, bid_add, ask_add)):
            raise ValueError("depth-event metrics must be finite")
        if scale < 0 or bid_add < 0 or ask_add < 0:
            raise ValueError("depth-event scale and replenishment must be non-negative")
        ofi_raw += raw
        ofi_scale += scale
        bid_replenishment += bid_add
        ask_replenishment += ask_add
        if index:
            previous = events[index - 1][1]
            previous_final = previous.get("final_update_id")
            current_previous = event.get("previous_update_id")
            if (
                previous_final is not None
                and current_previous is not None
                and int(current_previous) != int(previous_final)
            ):
                sequence_valid = False
    if not sequence_valid:
        diagnostics.append("depth_sequence_gap")
    ofi_ratio = ofi_raw / ofi_scale if ofi_scale > 0 else 0.0
    ofi_score = max(0.0, min(10.0, 5.0 + 50.0 * ofi_ratio))
    latest_times = [item[0] for item in trades]
    latest_times.extend(timestamp for timestamp, _ in events)
    latest = max(latest_times) if latest_times else None
    fresh = bool(
        len(trades) >= min_trade_events
        and len(events) >= min_depth_events
        and sequence_valid
        and latest is not None
        and as_of - latest <= max_staleness_ms
    )
    confidence = (
        min(
            1.0,
            len(trades) / float(min_trade_events),
            len(events) / float(min_depth_events),
        )
        if fresh
        else 0.0
    )
    if unclassified:
        diagnostics.append("unclassified_trade_side_excluded")
    if len(trades) < min_trade_events:
        diagnostics.append("insufficient_classified_trade_events")
    if len(events) < min_depth_events:
        diagnostics.append("insufficient_sequenced_depth_events")
    if latest is None or as_of - latest > max_staleness_ms:
        diagnostics.append("event_flow_stale")
    evidence = OrderFlowEvidence(
        cvd_score=cvd_score,
        ofi_score=ofi_score,
        fresh=fresh,
        confidence=confidence,
        bid_replenishment=min(10.0, bid_replenishment),
        ask_replenishment=min(10.0, ask_replenishment),
        aggressive_buy_ratio=(
            sum(
                price * quantity
                for _, side, price, quantity in trades
                if side == "BUY"
            )
            / total_notional
            if total_notional > 0
            else 0.5
        ),
    )
    return EventFlowResult(
        evidence=evidence,
        trade_count=len(trades),
        depth_update_count=len(events),
        duplicate_trade_count=duplicates,
        unclassified_trade_count=unclassified,
        latest_event_ms=latest,
        event_window_start_ms=start,
        diagnostics=tuple(diagnostics),
    )


def build_event_aligned_order_flow(
    trades: Iterable[AMDTradePrint],
    depth_snapshots: Iterable[AMDDepthSnapshot],
    *,
    symbol: str,
    as_of_ms: int,
    window_ms: int = 60_000,
    max_staleness_ms: int = 5_000,
    min_trade_events: int = 10,
    min_depth_updates: int = 2,
) -> EventFlowResult:
    """Build CVD/OFI from explicitly classified, event-time aligned inputs.

    Aggressor side must be supplied by the exchange adapter. Unknown-side prints
    are excluded rather than guessed from candle direction or quote midpoint.
    Depth refill is only a displayed-size increase at an unchanged best price;
    it is not proof of cancellations, hidden liquidity, or absorption.
    """
    as_of = _validate_timestamp(as_of_ms)
    if window_ms <= 0 or max_staleness_ms < 0:
        raise ValueError("window_ms must be positive and staleness non-negative")
    if min_trade_events < 1 or min_depth_updates < 1:
        raise ValueError("minimum event counts must be positive")
    start = max(0, as_of - int(window_ms))
    diagnostics: list[str] = []

    unique: dict[str, AMDTradePrint] = {}
    duplicates = 0
    unclassified = 0
    for trade in trades:
        timestamp = _validate_timestamp(trade.timestamp_ms)
        if timestamp < start or timestamp > as_of:
            continue
        if not isfinite(float(trade.price)) or not isfinite(float(trade.quantity)):
            raise ValueError("trade price and quantity must be finite")
        if trade.price <= 0 or trade.quantity <= 0:
            raise ValueError("trade price and quantity must be positive")
        key = str(trade.trade_id)
        if key in unique:
            duplicates += 1
            continue
        side = str(trade.aggressor_side or "").upper()
        if side not in {"BUY", "SELL"}:
            unclassified += 1
        unique[key] = trade

    classified = [
        item for item in unique.values()
        if str(item.aggressor_side or "").upper() in {"BUY", "SELL"}
    ]
    classified.sort(key=lambda item: (item.timestamp_ms, str(item.trade_id)))
    signed_notional = sum(
        (1.0 if item.aggressor_side.upper() == "BUY" else -1.0)
        * item.price * item.quantity
        for item in classified
    )
    total_notional = sum(item.price * item.quantity for item in classified)
    cvd_ratio = signed_notional / total_notional if total_notional > 0 else 0.0
    cvd_score = max(0.0, min(10.0, 5.0 + 5.0 * cvd_ratio))

    books = [
        item for item in depth_snapshots
        if start <= _validate_timestamp(item.timestamp_ms) <= as_of
    ]
    books.sort(key=lambda item: item.timestamp_ms)
    if any(a.timestamp_ms == b.timestamp_ms for a, b in zip(books, books[1:])):
        diagnostics.append("duplicate_depth_timestamps")
        books = [
            item for index, item in enumerate(books)
            if index == 0 or item.timestamp_ms != books[index - 1].timestamp_ms
        ]

    ofi_total = 0.0
    ofi_scale = 0.0
    bid_add = 0.0
    ask_add = 0.0
    updates = 0
    for previous, current in zip(books, books[1:]):
        delta, bid_refill, ask_refill = _book_ofi(previous, current)
        ofi_total += delta
        ofi_scale += abs(delta)
        bid_add += bid_refill
        ask_add += ask_refill
        updates += 1
    ofi_ratio = ofi_total / ofi_scale if ofi_scale > 0 else 0.0
    ofi_score = max(0.0, min(10.0, 5.0 + 5.0 * ofi_ratio))

    latest_times = [item.timestamp_ms for item in classified]
    if books:
        latest_times.append(books[-1].timestamp_ms)
    latest = max(latest_times) if latest_times else None
    fresh = bool(
        classified
        and updates >= min_depth_updates
        and latest is not None
        and as_of - latest <= max_staleness_ms
        and len(classified) >= min_trade_events
    )
    trade_coverage = min(1.0, len(classified) / float(min_trade_events))
    depth_coverage = min(1.0, updates / float(min_depth_updates))
    confidence = min(trade_coverage, depth_coverage) if fresh else 0.0
    if unclassified:
        diagnostics.append("unclassified_aggressor_side_excluded")
    if len(classified) < min_trade_events:
        diagnostics.append("insufficient_classified_trade_events")
    if updates < min_depth_updates:
        diagnostics.append("insufficient_depth_updates")
    if latest is None or as_of - latest > max_staleness_ms:
        diagnostics.append("event_flow_stale")

    evidence = OrderFlowEvidence(
        cvd_score=cvd_score,
        ofi_score=ofi_score,
        fresh=fresh,
        confidence=confidence,
        bid_replenishment=min(10.0, bid_add),
        ask_replenishment=min(10.0, ask_add),
        aggressive_buy_ratio=(
            sum(
                item.price * item.quantity
                for item in classified
                if item.aggressor_side.upper() == "BUY"
            )
            / total_notional
            if total_notional > 0 else 0.5
        ),
    )
    return EventFlowResult(
        evidence=evidence,
        trade_count=len(classified),
        depth_update_count=updates,
        duplicate_trade_count=duplicates,
        unclassified_trade_count=unclassified,
        latest_event_ms=latest,
        event_window_start_ms=start,
        diagnostics=tuple(diagnostics),
    )


@dataclass(frozen=True)
class AMDForwardOutcome:
    symbol: str
    timeframe: str
    state_id: str
    direction: str
    entry_time_ms: int
    entry_price: float
    horizon_bars: int
    observed_bars: int
    forward_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    outcome_complete: bool
    provenance: str = "COUNTERFACTUAL_CANDLE_MARKOUT"


def evaluate_amd_forward_outcome(
    state: Any,
    future_candles: Sequence[Any],
    *,
    entry_price: float,
    entry_time_ms: int,
    horizon_bars: int = 20,
) -> AMDForwardOutcome:
    """Label a future path without pretending a candle path gives fill order."""
    direction = str(state.direction).upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("AMD forward outcome requires LONG or SHORT direction")
    if not isfinite(float(entry_price)) or entry_price <= 0:
        raise ValueError("entry_price must be finite and positive")
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be positive")
    selected = tuple(future_candles[:horizon_bars])

    for row in selected:
        raw_timestamp = (
            row.get("timestamp_ms", row.get("timestamp", row.get("close_time")))
            if isinstance(row, Mapping)
            else getattr(
                row,
                "timestamp_ms",
                getattr(row, "timestamp", getattr(row, "close_time", None)),
            )
        )
        if raw_timestamp is None:
            continue
        if isinstance(raw_timestamp, str):
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            candle_time_ms = int(parsed.timestamp() * 1000)
        else:
            candle_time_ms = _event_time_ms(raw_timestamp)
        if candle_time_ms <= _validate_timestamp(entry_time_ms):
            raise ValueError("future outcome candle must occur after the AMD event")

    def field_value(row: Any, name: str) -> float:
        if isinstance(row, Mapping):
            return float(row[name])
        return float(getattr(row, name))

    highs = [field_value(row, "high") for row in selected]
    lows = [field_value(row, "low") for row in selected]
    closes = [field_value(row, "close") for row in selected]
    if any(not isfinite(value) for value in highs + lows + closes):
        raise ValueError("future candle prices must be finite")
    if any(low <= 0 or high < low for high, low in zip(highs, lows)):
        raise ValueError("invalid future candle range")
    sign = 1.0 if direction == "LONG" else -1.0
    favorable = max(
        (sign * (high - entry_price) / entry_price * 100.0 for high in highs)
        if direction == "LONG"
        else (sign * (low - entry_price) / entry_price * 100.0 for low in lows),
        default=None,
    )
    adverse = min(
        (sign * (low - entry_price) / entry_price * 100.0 for low in lows)
        if direction == "LONG"
        else (sign * (high - entry_price) / entry_price * 100.0 for high in highs),
        default=None,
    )
    close_return = (
        sign * (closes[-1] - entry_price) / entry_price * 100.0
        if closes else None
    )
    return AMDForwardOutcome(
        symbol=str(state.symbol).upper(),
        timeframe=str(state.timeframe).lower(),
        state_id=str(state.state_id),
        direction=direction,
        entry_time_ms=_validate_timestamp(entry_time_ms),
        entry_price=float(entry_price),
        horizon_bars=horizon_bars,
        observed_bars=len(selected),
        forward_return_pct=close_return,
        mfe_pct=favorable,
        mae_pct=adverse,
        outcome_complete=len(selected) == horizon_bars,
    )


@dataclass(frozen=True)
class WalkForwardFold:
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int


def purged_walk_forward_folds(
    event_times_ms: Sequence[int],
    *,
    min_train_size: int,
    test_size: int,
    step_size: int | None = None,
    embargo_bars: int = 0,
) -> tuple[WalkForwardFold, ...]:
    """Create chronological expanding folds with an explicit purge/embargo."""
    times = tuple(_validate_timestamp(value) for value in event_times_ms)
    if any(a >= b for a, b in zip(times, times[1:])):
        raise ValueError("event_times_ms must be strictly chronological")
    step = test_size if step_size is None else step_size
    if min_train_size < 1 or test_size < 1 or step < 1 or embargo_bars < 0:
        raise ValueError("fold sizes must be positive and embargo non-negative")
    folds: list[WalkForwardFold] = []
    test_start = min_train_size + embargo_bars
    while test_start + test_size <= len(times):
        train_end = test_start - embargo_bars
        train = tuple(range(train_end))
        test = tuple(range(test_start, test_start + test_size))
        folds.append(
            WalkForwardFold(
                train_indices=train,
                test_indices=test,
                train_end_ms=times[train[-1]],
                test_start_ms=times[test[0]],
                test_end_ms=times[test[-1]],
            )
        )
        test_start += step
    return tuple(folds)


def summarize_amd_cohorts(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Summarize already-labelled cohorts; no missing outcomes are imputed."""
    cohorts: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    counts: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    for row in rows:
        variant = str(row.get("variant", "unknown"))
        symbol = str(row.get("symbol", "unknown")).upper()
        regime = str(row.get("regime", "unknown"))
        phase = str(row.get("phase", "unknown"))
        direction = str(row.get("direction", "unknown")).upper()
        key = (variant, symbol, regime, phase, direction)
        counts[key] += 1
        value = row.get("net_return_pct")
        if value is not None:
            numeric = float(value)
            if not isfinite(numeric):
                raise ValueError("net_return_pct must be finite")
            cohorts[key].append(numeric)
    output = []
    for key in sorted(counts):
        values = cohorts[key]
        output.append({
            "variant": key[0],
            "symbol": key[1],
            "regime": key[2],
            "phase": key[3],
            "direction": key[4],
            "observations": counts[key],
            "resolved_outcomes": len(values),
            "unresolved_outcomes": counts[key] - len(values),
            "mean_net_return_pct": sum(values) / len(values) if values else None,
            "win_rate": (
                sum(value > 0 for value in values) / len(values)
                if values
                else None
            ),
            "profit_factor": (
                sum(value for value in values if value > 0)
                / abs(sum(value for value in values if value < 0))
                if any(value < 0 for value in values)
                else None
            ),
        })
    return tuple(output)


@dataclass(frozen=True)
class AMDExecutionReplay:
    direction: str
    quantity: float
    entry: ExecutionReplayResult
    exit: ExecutionReplayResult
    gross_pnl: float | None
    total_cost: float
    net_pnl: float | None
    net_return_pct: float | None
    complete: bool


def replay_amd_execution(
    entry_books: Iterable[BookSnapshot],
    exit_books: Iterable[BookSnapshot],
    *,
    direction: str,
    quantity: float,
    entry_submitted_at_ms: int,
    exit_submitted_at_ms: int,
    latency_ms: int,
    taker_fee_bps: float,
    slippage_bps: float = 0.0,
    funding_bps: float = 0.0,
    holding_hours: float = 0.0,
) -> AMDExecutionReplay:
    """Replay both legs with L1 fills and explicit venue-cost assumptions."""
    side = direction.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    entry_side = "BUY" if side == "LONG" else "SELL"
    exit_side = "SELL" if side == "LONG" else "BUY"
    entry = replay_historical_order(
        entry_books,
        side=entry_side,
        quantity=quantity,
        submitted_at_ms=entry_submitted_at_ms,
        latency_ms=latency_ms,
        taker_fee_bps=taker_fee_bps,
        slippage_bps=slippage_bps,
        funding_bps=funding_bps,
        holding_hours=holding_hours,
    )
    exit_quantity = entry.filled_quantity
    exit_result = (
        replay_historical_order(
            exit_books,
            side=exit_side,
            quantity=exit_quantity,
            submitted_at_ms=exit_submitted_at_ms,
            latency_ms=latency_ms,
            taker_fee_bps=taker_fee_bps,
            slippage_bps=slippage_bps,
        )
        if exit_quantity > 0
        else ExecutionReplayResult(
            exit_side,
            0.0,
            0.0,
            0.0,
            None,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            (),
            latency_ms,
            False,
        )
    )
    entry_remaining = [fill.quantity for fill in entry.fills]
    exit_remaining = [fill.quantity for fill in exit_result.fills]
    entry_index = 0
    exit_index = 0
    matched = 0.0
    gross = 0.0
    entry_notional = 0.0
    while entry_index < len(entry.fills) and exit_index < len(exit_result.fills):
        entry_fill = entry.fills[entry_index]
        exit_fill = exit_result.fills[exit_index]
        quantity_match = min(
            entry_remaining[entry_index], exit_remaining[exit_index]
        )
        matched += quantity_match
        entry_notional += quantity_match * entry_fill.fill_price
        gross += (
            (exit_fill.fill_price - entry_fill.fill_price) * quantity_match
            if side == "LONG"
            else (entry_fill.fill_price - exit_fill.fill_price) * quantity_match
        )
        entry_remaining[entry_index] -= quantity_match
        exit_remaining[exit_index] -= quantity_match
        if entry_remaining[entry_index] <= 1e-12:
            entry_index += 1
        if exit_remaining[exit_index] <= 1e-12:
            exit_index += 1
    gross_value = gross if matched > 0 else None
    allocated_entry_cost = (
        entry.total_cost * matched / entry.filled_quantity
        if entry.filled_quantity > 0
        else 0.0
    )
    cost = allocated_entry_cost + exit_result.total_cost
    complete = (
        entry.complete
        and exit_result.complete
        and matched >= quantity - 1e-12
    )
    net = gross_value - cost if gross_value is not None else None
    return AMDExecutionReplay(
        direction=side,
        quantity=quantity,
        entry=entry,
        exit=exit_result,
        gross_pnl=gross_value,
        total_cost=cost,
        net_pnl=net,
        net_return_pct=(
            net / entry_notional * 100.0
            if net is not None and entry_notional > 0
            else None
        ),
        complete=complete,
    )
