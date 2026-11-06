"""Paired hold-only versus capital-rebalancing historical evaluation.

Frames separate ex-ante decision inputs from next-period realized returns. This
prevents the replay harness from using a future return to make the switch
decision. The caller is responsible for constructing frames from a strictly
time-ordered, point-in-time feature/market dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping, Sequence

from aitos.intelligence.capital_rebalancing import (
    DynamicRebalancer,
    IncumbentPosition,
    RebalanceCandidate,
    RebalanceContext,
)


@dataclass(frozen=True, slots=True)
class CapitalRebalanceReplayFrame:
    timestamp: datetime
    incumbent: IncumbentPosition
    candidate: RebalanceCandidate
    context: RebalanceContext
    realized_returns_pct: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class PairedReplayMetrics:
    initial_equity: float
    final_equity: float
    net_return_pct: float
    max_drawdown_pct: float
    switches: int
    transaction_cost_pct: float
    fee_cost_pct: float
    slippage_cost_pct: float
    funding_cost_pct: float
    positive_advantage_vetoes: int
    observations: int


@dataclass(frozen=True, slots=True)
class CapitalRebalanceReplayResult:
    hold_only: PairedReplayMetrics
    rebalanced: PairedReplayMetrics
    incremental_return_pct: float
    incremental_equity: float
    switch_log: tuple[tuple[datetime, str, str, float], ...]


def _metrics(
    initial_equity: float,
    curve: Sequence[float],
    switches: int,
    transaction_cost_pct: float,
    fee_cost_pct: float,
    slippage_cost_pct: float,
    funding_cost_pct: float,
    positive_advantage_vetoes: int,
) -> PairedReplayMetrics:
    final = curve[-1] if curve else initial_equity
    peak = initial_equity
    drawdown = 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak * 100.0)
    return PairedReplayMetrics(
        initial_equity=initial_equity,
        final_equity=final,
        net_return_pct=(final / initial_equity - 1.0) * 100.0,
        max_drawdown_pct=drawdown,
        switches=switches,
        transaction_cost_pct=transaction_cost_pct,
        fee_cost_pct=fee_cost_pct,
        slippage_cost_pct=slippage_cost_pct,
        funding_cost_pct=funding_cost_pct,
        positive_advantage_vetoes=positive_advantage_vetoes,
        observations=len(curve),
    )


class CapitalRebalanceReplay:
    """Run paired strategies on the same ex-ante decisions and realized tape."""

    def __init__(self, evaluator: DynamicRebalancer | None = None) -> None:
        self.evaluator = evaluator or DynamicRebalancer()

    def run(
        self,
        frames: Sequence[CapitalRebalanceReplayFrame],
        *,
        initial_equity: float,
        final_exit_fee_bps: float = 0.0,
        final_exit_slippage_bps: float = 0.0,
    ) -> CapitalRebalanceReplayResult:
        if not isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("initial_equity must be finite and positive")
        for name, value in (
            ("final_exit_fee_bps", final_exit_fee_bps),
            ("final_exit_slippage_bps", final_exit_slippage_bps),
        ):
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        ordered = list(frames)
        if any(
            ordered[index].timestamp > ordered[index + 1].timestamp
            for index in range(len(ordered) - 1)
        ):
            raise ValueError("replay frames must be timestamp ordered")
        if not ordered:
            empty = _metrics(initial_equity, (), 0, 0.0, 0.0, 0.0, 0.0, 0)
            return CapitalRebalanceReplayResult(empty, empty, 0.0, 0.0, ())

        baseline_symbol = ordered[0].incumbent.symbol.upper()
        active_symbol = baseline_symbol
        hold_equity = rebalanced_equity = float(initial_equity)
        hold_curve: list[float] = []
        rebalanced_curve: list[float] = []
        switches = 0
        rebalance_fee_cost_pct = 0.0
        rebalance_slippage_cost_pct = 0.0
        rebalance_funding_cost_pct = 0.0
        positive_advantage_vetoes = 0
        switch_log: list[tuple[datetime, str, str, float]] = []

        for frame in ordered:
            incumbent_symbol = frame.incumbent.symbol.upper()
            candidate_symbol = frame.candidate.symbol.upper()
            if incumbent_symbol != active_symbol:
                raise ValueError(
                    "each frame's incumbent must match the currently active "
                    "rebalanced symbol; rebuild incumbent evidence point-in-time"
                )
            returns = frame.realized_returns_pct
            if baseline_symbol not in returns or active_symbol not in returns:
                raise ValueError(
                    "realized_returns_pct must contain both hold-only and active symbols"
                )
            baseline_return = float(returns[baseline_symbol])
            if not isfinite(baseline_return):
                raise ValueError("hold-only realized return must be finite")
            hold_equity *= 1.0 + baseline_return / 100.0
            hold_curve.append(hold_equity)

            decision = self.evaluator.evaluate(
                frame.incumbent, frame.candidate, frame.context
            )
            if decision.should_switch:
                old_symbol = active_symbol
                # The incumbent is exited and the replacement is entered at
                # this frame's decision boundary. Charge both sides' expected
                # execution costs once; funding is signed, so credits reduce cost.
                fee_cost_pct = (
                    frame.incumbent.exit_fee_bps + frame.candidate.fee_bps
                ) * 0.01
                slippage_cost_pct = (
                    frame.incumbent.exit_slippage_bps
                    + frame.candidate.slippage_bps
                ) * 0.01
                funding_cost_pct = frame.candidate.funding_bps * 0.01
                cost_pct = fee_cost_pct + slippage_cost_pct + funding_cost_pct
                if cost_pct >= 100.0:
                    raise ValueError("switch transaction cost would consume equity")
                rebalanced_equity *= 1.0 - cost_pct / 100.0
                rebalance_fee_cost_pct += fee_cost_pct
                rebalance_slippage_cost_pct += slippage_cost_pct
                rebalance_funding_cost_pct += funding_cost_pct
                active_symbol = candidate_symbol
                switches += 1
                switch_log.append(
                    (
                        frame.timestamp,
                        old_symbol,
                        active_symbol,
                        decision.net_advantage_pct,
                    )
                )
            elif candidate_symbol != active_symbol and decision.net_advantage_pct > 0:
                positive_advantage_vetoes += 1

            active_return = float(returns[active_symbol])
            if not isfinite(active_return) or active_return <= -100.0:
                raise ValueError(
                    "active realized return must be finite and above -100%"
                )
            if baseline_return <= -100.0:
                raise ValueError("hold-only realized return must be above -100%")
            rebalanced_equity *= 1.0 + active_return / 100.0
            rebalanced_curve.append(rebalanced_equity)

        final_fee_cost_pct = final_exit_fee_bps * 0.01
        final_slippage_cost_pct = final_exit_slippage_bps * 0.01
        final_cost_pct = final_fee_cost_pct + final_slippage_cost_pct
        if final_cost_pct >= 100.0:
            raise ValueError("final exit cost would consume equity")
        hold_equity *= 1.0 - final_cost_pct / 100.0
        # Candidate fee/slippage fields are the complete expected round trip,
        # already charged at each switch boundary. Avoid charging the final
        # switched position's exit a second time. If no switch occurred, both
        # paths are the same hold-only position and both pay the final exit.
        rebalanced_final_fee_cost_pct = final_fee_cost_pct if switches == 0 else 0.0
        rebalanced_final_slippage_cost_pct = (
            final_slippage_cost_pct if switches == 0 else 0.0
        )
        rebalanced_final_cost_pct = (
            rebalanced_final_fee_cost_pct + rebalanced_final_slippage_cost_pct
        )
        rebalanced_equity *= 1.0 - rebalanced_final_cost_pct / 100.0
        hold_curve[-1] = hold_equity
        rebalanced_curve[-1] = rebalanced_equity

        hold_metrics = _metrics(
            initial_equity,
            hold_curve,
            0,
            final_cost_pct,
            final_fee_cost_pct,
            final_slippage_cost_pct,
            0.0,
            0,
        )
        rebalanced_metrics = _metrics(
            initial_equity,
            rebalanced_curve,
            switches,
            (
                rebalance_fee_cost_pct
                + rebalance_slippage_cost_pct
                + rebalance_funding_cost_pct
                + rebalanced_final_cost_pct
            ),
            rebalance_fee_cost_pct + rebalanced_final_fee_cost_pct,
            rebalance_slippage_cost_pct + rebalanced_final_slippage_cost_pct,
            rebalance_funding_cost_pct,
            positive_advantage_vetoes,
        )
        return CapitalRebalanceReplayResult(
            hold_only=hold_metrics,
            rebalanced=rebalanced_metrics,
            incremental_return_pct=(
                rebalanced_metrics.net_return_pct - hold_metrics.net_return_pct
            ),
            incremental_equity=rebalanced_metrics.final_equity
            - hold_metrics.final_equity,
            switch_log=tuple(switch_log),
        )
