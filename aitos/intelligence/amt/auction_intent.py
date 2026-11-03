"""Responsive vs initiative auction classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .engine import AMTContext


class AuctionIntent(str, Enum):
    RESPONSIVE = "responsive"
    INITIATIVE = "initiative"
    MIXED = "mixed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuctionIntentResult:
    intent: AuctionIntent
    confidence: float
    evidence: tuple[str, ...]


def classify_auction_intent(context: AMTContext) -> AuctionIntentResult:
    evidence: list[str] = []
    initiative = 0.0
    responsive = 0.0
    if context.state.value in {"discovery_up", "discovery_down", "trend", "acceptance"}:
        initiative += 0.45
        evidence.append("price is discovering/accepting outside value")
    if context.state.value in {"rotation", "balance", "rejection"}:
        responsive += 0.35
        evidence.append("auction is rotating or rejecting extremes")
    if abs(context.book_imbalance) >= 0.20:
        initiative += 0.20
        evidence.append("order-book imbalance supports initiative flow")
    if context.rejection >= 0.30:
        responsive += 0.25
        evidence.append("recent return/rejection from value boundary")
    if context.acceptance >= 0.75 and context.price_location in (0.0, 1.0):
        initiative += 0.20
        evidence.append("sustained acceptance at value edge")
    total = initiative + responsive
    if total <= 0:
        return AuctionIntentResult(AuctionIntent.UNKNOWN, 0.0, tuple(evidence))
    if abs(initiative - responsive) < 0.15:
        intent = AuctionIntent.MIXED
    else:
        intent = (
            AuctionIntent.INITIATIVE
            if initiative > responsive
            else AuctionIntent.RESPONSIVE
        )
    return AuctionIntentResult(
        intent,
        round(min(1.0, abs(initiative - responsive) / max(total, 1e-9)), 4),
        tuple(evidence),
    )
