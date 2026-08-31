"""Trade Thesis Engine — build and evaluate thesis consistency.

Deterministic, explainable. No ML.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.market_state.models import (
    MarketState,
    MomentumState,
    OrderFlowBias,
    StructureBias,
)
from aitos.intelligence.trade_thesis.models import (
    ConfirmationSignal,
    InvalidationCondition,
    ThesisComponent,
    ThesisEvaluation,
    ThesisHealth,
    TradeThesis,
)
from aitos.logging_setup import get_logger

logger = get_logger("aitos.intelligence.trade_thesis")


class TradeThesisEngine:
    """Build a thesis at entry and evaluate it against live MarketState."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._cfg = dict(config or {})

    def build_from_entry(
        self,
        *,
        trade_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        market_state: MarketState | None = None,
        structural_invalidation_price: float | None = None,
        expected_path_prices: Sequence[float] = (),
        strategy_id: str = "",
        rationale: str = "",
        agent_consensus: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> TradeThesis:
        side = side.upper()
        ts = timestamp or datetime.now(timezone.utc)
        components: list[ThesisComponent] = []
        invalidations: list[InvalidationCondition] = []
        confirmations: list[ConfirmationSignal] = []
        notes: list[str] = []
        features: dict[str, float] = {"entry_price": entry_price}

        if market_state is not None:
            features["trend_strength"] = market_state.trend_strength
            features["reversal_risk"] = market_state.reversal_risk
            if side == "LONG" and market_state.structure == StructureBias.BULLISH:
                components.append(
                    ThesisComponent(
                        "bullish_structure",
                        "Entry aligned with bullish market structure",
                        1.2,
                    )
                )
                confirmations.append(
                    ConfirmationSignal(
                        "structure_supportive", "Structure remains BULLISH"
                    )
                )
            elif side == "SHORT" and market_state.structure == StructureBias.BEARISH:
                components.append(
                    ThesisComponent(
                        "bearish_structure",
                        "Entry aligned with bearish market structure",
                        1.2,
                    )
                )
                confirmations.append(
                    ConfirmationSignal(
                        "structure_supportive", "Structure remains BEARISH"
                    )
                )
            if (
                side == "LONG"
                and market_state.order_flow_bias == OrderFlowBias.BUYER_DOMINANT
            ):
                components.append(
                    ThesisComponent(
                        "buyer_imbalance", "Buyer-dominant order flow at entry", 1.0
                    )
                )
                confirmations.append(
                    ConfirmationSignal(
                        "of_supportive", "Order flow remains BUYER_DOMINANT"
                    )
                )
            elif (
                side == "SHORT"
                and market_state.order_flow_bias == OrderFlowBias.SELLER_DOMINANT
            ):
                components.append(
                    ThesisComponent(
                        "seller_imbalance", "Seller-dominant order flow at entry", 1.0
                    )
                )
                confirmations.append(
                    ConfirmationSignal(
                        "of_supportive", "Order flow remains SELLER_DOMINANT"
                    )
                )
            if market_state.momentum in (
                MomentumState.STRONG,
                MomentumState.MODERATING,
            ):
                components.append(
                    ThesisComponent(
                        "momentum_support",
                        f"Momentum {market_state.momentum.value} at entry",
                        0.8,
                    )
                )
                confirmations.append(
                    ConfirmationSignal(
                        "momentum_not_exhausted", "Momentum not EXHAUSTED"
                    )
                )
            notes.append(f"regime={market_state.regime.value}")

        if (
            structural_invalidation_price is not None
            and structural_invalidation_price > 0
        ):
            invalidations.append(
                InvalidationCondition(
                    "structure_break",
                    f"Price breaches structural level {structural_invalidation_price:.6g}",
                    level=structural_invalidation_price,
                )
            )
            notes.append(f"invalidation_price={structural_invalidation_price:.6g}")

        if side == "LONG":
            invalidations.append(
                InvalidationCondition(
                    "structure_against", "Structure flips to BEARISH or BROKEN"
                )
            )
            invalidations.append(
                InvalidationCondition(
                    "of_reversal", "Order flow flips to SELLER_DOMINANT"
                )
            )
        else:
            invalidations.append(
                InvalidationCondition(
                    "structure_against", "Structure flips to BULLISH or BROKEN"
                )
            )
            invalidations.append(
                InvalidationCondition(
                    "of_reversal", "Order flow flips to BUYER_DOMINANT"
                )
            )

        if strategy_id:
            notes.append(f"strategy={strategy_id}")
        if rationale:
            notes.append(rationale[:200])
        if not components:
            components.append(
                ThesisComponent("directional_bias", f"{side} directional entry", 0.5)
            )

        return TradeThesis(
            trade_id=trade_id,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            components=tuple(components),
            invalidations=tuple(invalidations),
            confirmations=tuple(confirmations),
            expected_path_prices=tuple(float(p) for p in expected_path_prices if p > 0),
            invalidation_price=structural_invalidation_price,
            created_at=ts,
            notes=tuple(notes),
            features=features,
        )

    def evaluate(
        self,
        thesis: TradeThesis,
        market_state: MarketState,
        current_price: float | None = None,
    ) -> ThesisEvaluation:
        price = (
            current_price
            if current_price and current_price > 0
            else market_state.mid_price
        )
        side = thesis.side
        breached: list[str] = []
        lost: list[str] = []
        active: list[str] = []
        notes: list[str] = []

        for inv in thesis.invalidations:
            if inv.code == "structure_break":
                level = inv.level or thesis.invalidation_price
                if level is not None and level > 0:
                    if side == "LONG" and price <= level or side == "SHORT" and price >= level:
                        breached.append(inv.code)
            elif inv.code == "structure_against":
                if market_state.structure == StructureBias.BROKEN or side == "LONG" and market_state.structure == StructureBias.BEARISH or (
                    side == "SHORT" and market_state.structure == StructureBias.BULLISH
                ):
                    breached.append(inv.code)
            elif inv.code == "of_reversal":
                if (
                    side == "LONG"
                    and market_state.order_flow_bias == OrderFlowBias.SELLER_DOMINANT
                ) or (
                    side == "SHORT"
                    and market_state.order_flow_bias == OrderFlowBias.BUYER_DOMINANT
                ):
                    breached.append(inv.code)

        for conf in thesis.confirmations:
            if conf.code == "structure_supportive":
                if (
                    side == "LONG" and market_state.structure == StructureBias.BULLISH
                ) or (
                    side == "SHORT" and market_state.structure == StructureBias.BEARISH
                ):
                    active.append(conf.code)
                else:
                    lost.append(conf.code)
            elif conf.code == "of_supportive":
                if (
                    side == "LONG"
                    and market_state.order_flow_bias == OrderFlowBias.BUYER_DOMINANT
                ) or (
                    side == "SHORT"
                    and market_state.order_flow_bias == OrderFlowBias.SELLER_DOMINANT
                ):
                    active.append(conf.code)
                else:
                    lost.append(conf.code)
            elif conf.code == "momentum_not_exhausted":
                if market_state.momentum != MomentumState.EXHAUSTED:
                    active.append(conf.code)
                else:
                    lost.append(conf.code)

        if breached:
            health = ThesisHealth.INVALIDATED
            consistency = 0.0
        elif lost and not active:
            health = ThesisHealth.DEGRADED
            consistency = 0.35
        elif lost:
            health = ThesisHealth.DEGRADED
            n_conf = max(1, len(thesis.confirmations))
            consistency = max(0.4, 1.0 - 0.4 * (len(lost) / n_conf))
        else:
            health = ThesisHealth.INTACT
            consistency = 1.0 if thesis.confirmations else 0.75

        if market_state.reversal_risk >= 0.55 and health != ThesisHealth.INVALIDATED:
            consistency = max(0.0, consistency - 0.15)
            if health == ThesisHealth.INTACT:
                health = ThesisHealth.DEGRADED

        return ThesisEvaluation(
            health=health,
            consistency_score=round(consistency, 4),
            breached_invalidations=tuple(breached),
            lost_confirmations=tuple(lost),
            active_confirmations=tuple(active),
            notes=tuple(notes),
        )
