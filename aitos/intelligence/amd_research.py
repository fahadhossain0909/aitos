"""Point-in-time AMD research, event-flow and execution-aware validation helpers.

These helpers produce research labels and replay measurements only. They do not
create trading signals, infer hidden queue position, or promote AMD into gates.
"""

from __future__ import annotations

from collections import defaultdict
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
        raise ValueError("depth levels must contain finite positive prices and non-negative size")
    return price, size


def _book_ofi(previous: AMDDepthSnapshot, current: AMDDepthSnapshot) -> tuple[float, float, float]:
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
            sum(item.price * item.quantity for item in classified if item.aggressor_side.upper() == "BUY")
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
    highs = [float(getattr(row, "high", row.get("high"))) for row in selected]
    lows = [float(getattr(row, "low", row.get("low"))) for row in selected]
    closes = [float(getattr(row, "close", row.get("close"))) for row in selected]
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
            "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
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
    exit_result = replay_historical_order(
        exit_books,
        side=exit_side,
        quantity=exit_quantity if exit_quantity > 0 else quantity,
        submitted_at_ms=exit_submitted_at_ms,
        latency_ms=latency_ms,
        taker_fee_bps=taker_fee_bps,
        slippage_bps=slippage_bps,
    )
    matched = min(entry.filled_quantity, exit_result.filled_quantity)
    gross = None
    if matched > 0 and entry.average_fill_price and exit_result.average_fill_price:
        gross = (
            (exit_result.average_fill_price - entry.average_fill_price) * matched
            if side == "LONG"
            else (entry.average_fill_price - exit_result.average_fill_price) * matched
        )
    cost = entry.total_cost + exit_result.total_cost
    complete = entry.complete and exit_result.complete and matched >= quantity - 1e-12
    net = gross - cost if gross is not None else None
    entry_notional = (
        entry.average_fill_price * matched
        if entry.average_fill_price is not None else 0.0
    )
    return AMDExecutionReplay(
        direction=side,
        quantity=quantity,
        entry=entry,
        exit=exit_result,
        gross_pnl=gross,
        total_cost=cost,
        net_pnl=net,
        net_return_pct=(net / entry_notional * 100.0 if net is not None and entry_notional > 0 else None),
        complete=complete,
    )
