"""Explicit take-profit authority contract for lifecycle execution."""

from __future__ import annotations

from dataclasses import dataclass

from aitos.models.trade import TradeSide


@dataclass(frozen=True)
class TakeProfitExecutionContract:
    """Defines whether a TP level is an execution order or an intel checkpoint.

    In intelligent mode, static TP levels never submit a close by themselves:
    they trigger an immediate thesis/path evaluation and are consumed as
    checkpoints. Only an explicit Exit Intelligence EXIT decision may close.
    Legacy mode preserves the historical static TP behavior.
    """

    intelligent: bool

    def reached(self, side: TradeSide, price: float, target: float) -> bool:
        return price >= target if side == TradeSide.LONG else price <= target

    @property
    def tp_is_checkpoint(self) -> bool:
        return self.intelligent

    @property
    def static_tp_may_close(self) -> bool:
        return not self.intelligent

    def can_extend(self, side: TradeSide, current_target: float, proposed: float) -> bool:
        if current_target <= 0 or proposed <= 0:
            return False
        return proposed > current_target if side == TradeSide.LONG else proposed < current_target
