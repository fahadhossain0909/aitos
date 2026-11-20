"""Typed, non-executable bridge from StrategyEngine output to shadow proposals.

The bridge intentionally does not create TradeLifecycle orders. Strategy intents
lack canonical stop, account, venue and Opportunity semantics; these proposals
are evidence for review and evaluation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .contracts import StrategyContext
from .engine import StrategyCycle


@dataclass(frozen=True)
class ShadowStrategyProposal:
    strategy_id: str
    instrument_id: str
    side: str
    reference_price: float
    requested_notional: float
    allocated_notional: float
    projected_loss: float
    rationale: str
    execution_eligible: bool = False


@dataclass(frozen=True)
class StrategyShadowReport:
    generated_at_ns: int
    mode: str
    proposals: tuple[ShadowStrategyProposal, ...]
    rejected: tuple[tuple[str, str], ...]
    execution_eligible: bool = False


def build_strategy_shadow_report(
    cycle: StrategyCycle, context: StrategyContext
) -> StrategyShadowReport:
    """Map evidenced strategy intents to non-executable shadow proposals.

    Invalid, unpriced, unallocated, or unsupported intents are rejected rather
    than assigned guessed prices, sizes, stops, accounts, or venue semantics.
    """
    allocations = {item.strategy_id: item for item in cycle.allocations}
    if len(allocations) != len(cycle.allocations):
        raise ValueError("duplicate strategy allocation identity")

    proposals: list[ShadowStrategyProposal] = []
    rejected: list[tuple[str, str]] = []
    for result in cycle.results:
        allocation = allocations.get(result.strategy_id)
        for index, intent in enumerate(result.intents):
            key = f"{result.strategy_id}:{index}"
            snapshot = context.snapshots.get(intent.instrument_id)
            if snapshot is None:
                rejected.append((key, "missing point-in-time instrument snapshot"))
                continue
            if intent.side not in {"buy", "sell"}:
                rejected.append((key, "unsupported intent side"))
                continue
            if not isfinite(snapshot.mid) or snapshot.mid <= 0:
                rejected.append((key, "missing or invalid reference price"))
                continue
            if (
                allocation is None
                or not isfinite(allocation.notional)
                or allocation.notional <= 0
            ):
                rejected.append((key, "missing positive capital allocation"))
                continue
            if not isfinite(intent.quantity) or intent.quantity <= 0:
                rejected.append((key, "invalid intent quantity"))
                continue
            requested_notional = intent.quantity * snapshot.mid
            if not isfinite(requested_notional) or requested_notional <= 0:
                rejected.append((key, "invalid requested notional"))
                continue
            if not isfinite(allocation.projected_loss) or allocation.projected_loss < 0:
                rejected.append((key, "invalid allocated projected loss"))
                continue
            proposals.append(
                ShadowStrategyProposal(
                    strategy_id=result.strategy_id,
                    instrument_id=intent.instrument_id,
                    side="LONG" if intent.side == "buy" else "SHORT",
                    reference_price=float(snapshot.mid),
                    requested_notional=float(requested_notional),
                    allocated_notional=float(
                        min(requested_notional, allocation.notional)
                    ),
                    projected_loss=float(allocation.projected_loss),
                    rationale=str(intent.rationale)[:1000],
                )
            )
    return StrategyShadowReport(
        generated_at_ns=context.now_ns,
        mode=context.mode.value,
        proposals=tuple(proposals),
        rejected=tuple(rejected),
    )
