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
from typing import Sequence

from aitos.intelligence.order_flow import delta
from aitos.models.market import OrderBookSnapshot, TradeTick


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
    depth_per_price = (bid + ask) / (2 * mid)
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
    """Composite liquidity score used by the scanner."""
    scores = (
        liquidity_quality_score(book),
        depth_imbalance_score(book),
        liquidity_wall_score(book),
        sweep_potential_score(book),
        absorption_proxy_score(book, trades),
    )
    return round(sum(scores) / len(scores), 2)
