"""Order-book liquidity intelligence.

This module intentionally works with the data ProjectAlpha actually has:
a depth snapshot plus executed trades.  It detects *current* liquidity
walls, near-touch depth imbalance and a conservative absorption proxy.
It does not pretend a single snapshot can prove a historical sweep; sweep
confirmation requires a sequence of snapshots/trades and belongs in the
streaming/replay layer.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any

from aitos.intelligence.order_flow import delta
from aitos.models.market import OrderBookSnapshot, TradeSide, TradeTick


def _near_depth(book: OrderBookSnapshot, levels: int = 5) -> tuple[float, float]:
    bids = sum(q for _, q in book.bids[:levels])
    asks = sum(q for _, q in book.asks[:levels])
    return bids, asks


def liquidity_quality_score(
    book: OrderBookSnapshot, typical_spread_bps: float = 5.0
) -> float:
    """0-10 score combining spread quality and near-touch depth balance."""
    if not book.bids or not book.asks:
        return 0.0
    mid = (book.best_bid + book.best_ask) / 2
    if mid <= 0:
        return 0.0
    spread_bps = (book.spread / mid) * 10_000
    spread_score = max(0.0, min(10.0, 10.0 - (spread_bps / typical_spread_bps) * 5.0))
    bid_depth, ask_depth = _near_depth(book)
    total = bid_depth + ask_depth
    if total <= 0:
        return 0.0
    balance_score = 10.0 - abs(bid_depth - ask_depth) / total * 10.0
    return round((spread_score + balance_score) / 2, 2)


def depth_imbalance_score(book: OrderBookSnapshot, levels: int = 5) -> float:
    """Directional 0-10 score: 5 balanced, >5 bid-heavy, <5 ask-heavy."""
    bid, ask = _near_depth(book, levels)
    total = bid + ask
    return (
        5.0
        if total <= 0
        else round(max(0.0, min(10.0, 5.0 + 5.0 * (bid - ask) / total)), 2)
    )


def _max_level_qty(book: OrderBookSnapshot, levels: int = 10) -> tuple[float, float]:
    bid = max((q for _, q in book.bids[:levels]), default=0.0)
    ask = max((q for _, q in book.asks[:levels]), default=0.0)
    return bid, ask


def liquidity_wall_score(book: OrderBookSnapshot, levels: int = 10) -> float:
    """0-10 strength of visible liquidity walls relative to average depth."""
    bids = [q for _, q in book.bids[:levels]]
    asks = [q for _, q in book.asks[:levels]]
    values = bids + asks
    if not values:
        return 0.0
    avg = sum(values) / len(values)
    if avg <= 0:
        return 0.0
    max_qty = max(values)
    return round(max(0.0, min(10.0, max_qty / avg * 2.5)), 2)


def sweep_potential_score(book: OrderBookSnapshot, levels: int = 5) -> float:
    """0-10 estimate of how easily the near-touch book could be swept.

    Thin near-touch depth receives a high score.  This is *potential*, not
    confirmation of a completed sweep.
    """
    bid, ask = _near_depth(book, levels)
    mid = (book.best_bid + book.best_ask) / 2
    if mid <= 0 or bid + ask <= 0:
        return 10.0
    # Depth is reported in native quantity units; without contract-specific
    # notional normalization it is not comparable across symbols.
    # Relative quantity is not comparable across symbols, so use the
    # coefficient of variation of level quantities as a stability proxy.
    levels_qty = [q for _, q in book.bids[:levels]] + [q for _, q in book.asks[:levels]]
    if len(levels_qty) < 2:
        return 5.0
    mean = sum(levels_qty) / len(levels_qty)
    variance = sum((q - mean) ** 2 for q in levels_qty) / len(levels_qty)
    cv = math.sqrt(variance) / mean if mean else 10.0
    # High variability plus low depth balance implies fragile liquidity.
    return round(max(0.0, min(10.0, 5.0 + cv * 2.0)), 2)


def absorption_proxy_score(
    book: OrderBookSnapshot, trades: Sequence[TradeTick], levels: int = 5
) -> float:
    """0-10 proxy for aggressive flow meeting strong resting liquidity.

    A single snapshot cannot prove absorption.  This proxy only says whether
    aggressive traded volume is large while the corresponding near-touch side
    remains comparatively deep.
    """
    if not trades or not book.bids or not book.asks:
        return 5.0
    aggressive_delta = delta(trades)
    bid, ask = _near_depth(book, levels)
    dominant_depth = bid if aggressive_delta < 0 else ask
    opposing_depth = ask if aggressive_delta < 0 else bid
    total = bid + ask
    if total <= 0:
        return 5.0
    flow_strength = min(
        1.0, abs(aggressive_delta) / max(sum(abs(t.quantity) for t in trades), 1e-12)
    )
    depth_share = dominant_depth / total
    return round(
        max(0.0, min(10.0, 5.0 + (depth_share - 0.5) * 10.0 * flow_strength)), 2
    )


def liquidity_intelligence_score(
    book: OrderBookSnapshot, trades: Sequence[TradeTick] = ()
) -> float:
    """Execution-quality score only; directional/risk metrics stay separate.

    Sweep potential and absorption are adverse-selection diagnostics, not
    positive liquidity quality. Directional depth imbalance is also excluded
    because averaging it into quality biases LONG and SHORT symmetrically.
    """
    return liquidity_quality_score(book)


def order_book_flow_imbalance(
    previous: OrderBookSnapshot | None,
    current: OrderBookSnapshot,
    *,
    levels: int = 5,
) -> float:
    """Return a normalized 0-10 best-quote OFI score (5 = neutral).

    This is a consecutive-snapshot event measure, not a static depth ratio.
    Positive values indicate bid-side pressure; negative values ask-side.
    """
    if (
        previous is None
        or not previous.bids
        or not previous.asks
        or not current.bids
        or not current.asks
    ):
        return 5.0
    pb, pq = previous.bids[0]
    pa, aq = previous.asks[0]
    cb, cq = current.bids[0]
    ca, cqa = current.asks[0]
    bid_flow = cq if cb > pb else (cq - pq if cb == pb else -pq)
    ask_flow = -cqa if ca < pa else (-(cqa - aq) if ca == pa else aq)
    raw = bid_flow + ask_flow
    scale = sum(q for _, q in previous.bids[:levels] + previous.asks[:levels])
    scale += sum(q for _, q in current.bids[:levels] + current.asks[:levels])
    normalized = raw / max(scale, 1e-12)
    return round(max(0.0, min(10.0, 5.0 + 5.0 * normalized * levels)), 2)


def depth_event_flow_analysis(
    events: Sequence[dict[str, Any]],
    *,
    now_ms: int | None = None,
    max_age_ms: int = 1500,
    min_fresh_events: int = 3,
) -> dict[str, float | bool]:
    """Aggregate depth OFI only from a sufficiently fresh event window.

    Legacy events without timestamps retain their historical score for API
    compatibility, but are explicitly marked unavailable so the scanner never
    treats them as a trustworthy live OFI component.
    """
    if max_age_ms <= 0 or min_fresh_events < 1:
        raise ValueError("OFI freshness limits must be positive")
    if not events:
        return {
            "ofi_score": 5.0,
            "ofi_confidence": 0.0,
            "ofi_available": False,
            "freshness_ms": -1.0,
            "fresh_event_count": 0.0,
            "stale_event_count": 0.0,
            "timestamped_event_count": 0.0,
            "bid_add_qty": 0.0,
            "bid_reduce_qty": 0.0,
            "ask_add_qty": 0.0,
            "ask_reduce_qty": 0.0,
            "inferred_cancel_qty": 0.0,
            "cancellation_proxy_qty": 0.0,
            "matched_execution_qty": 0.0,
            "matched_bid_execution_qty": 0.0,
            "matched_ask_execution_qty": 0.0,
            "bid_cancellation_proxy_qty": 0.0,
            "ask_cancellation_proxy_qty": 0.0,
            "matched_trade_count": 0.0,
            "attribution_pending_qty": 0.0,
            "execution_attributed_ratio": 0.0,
            "replenishment_qty": 0.0,
            "bid_replenishment_qty": 0.0,
            "ask_replenishment_qty": 0.0,
            "bid_replenishment_ratio": 0.0,
            "ask_replenishment_ratio": 0.0,
            "replenishment_ratio": 0.0,
            "inferred_cancel_pressure": 0.0,
            "bid_inferred_cancel_pressure": 0.0,
            "ask_inferred_cancel_pressure": 0.0,
            "addition_qty": 0.0,
            "event_count": 0.0,
        }

    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    timestamped: list[tuple[dict[str, Any], float]] = []
    fresh_events: list[dict[str, Any]] = []
    stale_count = 0
    for event in events:
        try:
            event_time = float(event.get("event_time_ms"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(event_time) or event_time <= 0:
            continue
        timestamped.append((event, event_time))
        age_ms = current_ms - event_time
        if 0 <= age_ms <= max_age_ms:
            fresh_events.append(event)
        elif age_ms > max_age_ms:
            stale_count += 1

    if timestamped:
        calculation_events = fresh_events
        newest_fresh = max(
            (float(event["event_time_ms"]) for event in fresh_events),
            default=0.0,
        )
        freshness_ms = (
            float(max(0, current_ms - newest_fresh))
            if newest_fresh
            else float(max_age_ms + 1)
        )
        freshness_factor = (
            max(0.0, 1.0 - freshness_ms / max_age_ms) if fresh_events else 0.0
        )
        sample_factor = min(1.0, len(fresh_events) / min_fresh_events)
        confidence = round(sample_factor * freshness_factor, 4)
    else:
        # Preserve historical aggregate values for offline/legacy callers. The
        # missing timestamps still make the live confidence contract unavailable.
        calculation_events = list(events)
        freshness_ms = -1.0
        confidence = 0.0

    raw_ofi = sum(e.get("ofi_raw", 0.0) for e in calculation_events)
    depth_scale = sum(e.get("ofi_scale", 0.0) for e in calculation_events)
    normalized = raw_ofi / max(depth_scale, 1e-12)
    totals = {
        key: sum(max(0.0, e.get(key, 0.0)) for e in calculation_events)
        for key in (
            "bid_add_qty",
            "bid_reduce_qty",
            "ask_add_qty",
            "ask_reduce_qty",
            "replenishment_qty",
            "bid_replenishment_qty",
            "ask_replenishment_qty",
            "addition_qty",
            "matched_execution_qty",
            "matched_bid_execution_qty",
            "matched_ask_execution_qty",
            "bid_cancellation_proxy_qty",
            "ask_cancellation_proxy_qty",
            "attribution_pending_qty",
        )
    }
    matched_trade_ids = {
        trade_id
        for event in calculation_events
        for trade_id in event.get("matched_trade_ids", ())
    }
    cancellation_proxy = sum(
        max(
            0.0,
            event.get(
                "cancellation_proxy_qty",
                event.get("inferred_cancel_qty", 0.0),
            ),
        )
        for event in calculation_events
    )
    reduction_total = sum(
        max(
            0.0,
            e.get("reduction_qty", e.get("inferred_cancel_qty", 0.0)),
        )
        for e in calculation_events
    )
    totals.update(
        {
            "cancellation_proxy_qty": cancellation_proxy,
            "inferred_cancel_qty": cancellation_proxy,
            "matched_trade_count": float(len(matched_trade_ids)),
            "ofi_score": (
                round(max(0.0, min(10.0, 5.0 + 50.0 * normalized)), 2)
                if calculation_events
                else 5.0
            ),
            "ofi_confidence": confidence,
            "ofi_available": bool(
                timestamped
                and len(fresh_events) >= min_fresh_events
                and depth_scale > 0
                and confidence > 0
            ),
            "freshness_ms": freshness_ms,
            "fresh_event_count": float(len(fresh_events)),
            "stale_event_count": float(stale_count),
            "timestamped_event_count": float(len(timestamped)),
            "replenishment_ratio": round(
                totals["replenishment_qty"] / max(reduction_total, 1e-12), 4
            ),
            "bid_replenishment_ratio": round(
                totals["bid_replenishment_qty"] / max(totals["bid_reduce_qty"], 1e-12),
                4,
            ),
            "ask_replenishment_ratio": round(
                totals["ask_replenishment_qty"] / max(totals["ask_reduce_qty"], 1e-12),
                4,
            ),
            "inferred_cancel_pressure": round(
                cancellation_proxy
                / max(cancellation_proxy + totals["addition_qty"], 1e-12),
                4,
            ),
            # A side-specific reduction is suspicious only to the extent that
            # matched executions do not explain it. This keeps a global cancel
            # ratio from incorrectly vetoing the opposite trade direction.
            "bid_inferred_cancel_pressure": round(
                totals["bid_cancellation_proxy_qty"]
                / max(
                    totals["bid_cancellation_proxy_qty"]
                    + totals["matched_bid_execution_qty"],
                    1e-12,
                ),
                4,
            ),
            "ask_inferred_cancel_pressure": round(
                totals["ask_cancellation_proxy_qty"]
                / max(
                    totals["ask_cancellation_proxy_qty"]
                    + totals["matched_ask_execution_qty"],
                    1e-12,
                ),
                4,
            ),
            "execution_attributed_ratio": round(
                totals["matched_execution_qty"] / max(reduction_total, 1e-12), 4
            ),
            "event_count": float(len(events)),
        }
    )
    return totals


def adverse_flow_without_progress(
    trades: Sequence[TradeTick],
    direction: str,
    *,
    min_trades: int = 20,
    min_dominance: float = 0.65,
    max_progress_bps: float = 1.0,
) -> bool:
    """Detect aggressive flow that fails to make directional price progress.

    This conservative veto requires enough executions and a clear dominant
    aggressor; it is a warning, not proof of a hidden institutional order.
    """
    if len(trades) < min_trades:
        return False
    buy_qty = sum(
        abs(t.quantity)
        for t in trades
        if not t.is_buyer_maker and t.side != TradeSide.SELL
    )
    sell_qty = sum(
        abs(t.quantity) for t in trades if t.is_buyer_maker or t.side == TradeSide.SELL
    )
    total = buy_qty + sell_qty
    if total <= 0:
        return False
    first, last = float(trades[0].price), float(trades[-1].price)
    if first <= 0:
        return False
    progress_bps = (last - first) / first * 10_000
    side = direction.lower()
    if side == "long":
        return buy_qty / total >= min_dominance and progress_bps <= max_progress_bps
    if side == "short":
        return sell_qty / total >= min_dominance and progress_bps >= -max_progress_bps
    return False
