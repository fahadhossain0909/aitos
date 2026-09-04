"""Bridge the existing opportunity scanner into contextual intelligence."""

from __future__ import annotations

from typing import Any

from aitos.intelligence.advanced_context import AdvancedMarketContext, build_advanced_context
from aitos.intelligence.contextual_layers import PositioningContext
from aitos.intelligence.graph_context import retrieve_graph_context

_PATCHED = "_aitos_contextual_bridge_patched"
_ORIGINAL_SCAN = "_aitos_original_scan_symbol"
_ORIGINAL_DECIDE = "_aitos_original_decide_with_kernel"


def install_contextual_scanner_bridge(scanner_cls: type[Any]) -> None:
    """Install contextual enrichment on OpportunityScanner exactly once."""
    if getattr(scanner_cls, _PATCHED, False):
        return
    original_scan = scanner_cls.scan_symbol
    original_decide = scanner_cls.decide_with_kernel
    setattr(scanner_cls, _ORIGINAL_SCAN, original_scan)
    setattr(scanner_cls, _ORIGINAL_DECIDE, original_decide)

    async def scan_symbol(self: Any, symbol: str, reference_klines: list | None = None):
        candidate = await original_scan(self, symbol, reference_klines)
        if candidate is None:
            return None
        try:
            klines = await self._exchange.fetch_klines(symbol, self._timeframe, limit=self._kline_lookback)
            if len(klines) >= 20:
                flow_score = float(candidate.component_scores.get("order_flow_bias", 5.0))
                advanced = build_advanced_context(klines, current_cvd_score=flow_score, oi_change=None)
                object.__setattr__(candidate, "_aitos_klines", tuple(klines))
                object.__setattr__(candidate, "_aitos_advanced_context", advanced)
        except Exception:
            pass
        return candidate

    async def decide_with_kernel(self: Any, candidate: Any, kernel: Any):
        from aitos.kernel.ai_kernel import DecisionContext

        context_scores = dict(candidate.component_scores)
        advanced = getattr(candidate, "_aitos_advanced_context", None)
        if isinstance(advanced, AdvancedMarketContext):
            if advanced.volume_profile is not None:
                context_scores["volume_profile"] = round(advanced.volume_profile.price_location * 10.0, 4)
            context_scores["price_imbalance"] = round(5.0 + advanced.imbalance.displacement_score * 5.0, 4)
            context_scores["structural_symmetry"] = round(5.0 + (advanced.symmetry.similarity * 5.0 if advanced.symmetry else 0.0), 4)
            context_scores["forced_flow"] = round(5.0 + min(5.0, advanced.forced_flow_score / 2.0), 4)

        availability = dict(candidate.component_availability)
        if isinstance(advanced, AdvancedMarketContext):
            availability.update({
                "volume_profile": advanced.volume_profile is not None,
                "price_imbalance": bool(advanced.imbalance.zones),
                "structural_symmetry": advanced.symmetry is not None,
                "forced_flow": True,
            })

        positioning: PositioningContext | None = getattr(candidate, "positioning", None)
        graph_context = await retrieve_graph_context(
            symbol=candidate.symbol,
            regime=str(candidate.regime),
            direction=candidate.direction.value,
            strategy_id=str(getattr(candidate, "strategy_id", "") or ""),
            model_id=str(getattr(candidate, "model_id", "") or ""),
            limit=12,
        )
        context_scores["graph_historical_support"] = graph_context["score"]
        availability["graph_historical_support"] = graph_context["available"]
        context = {
            "direction": candidate.direction.value,
            "component_scores": context_scores,
            "component_availability": availability,
            "regime": candidate.regime,
            "entry_price": candidate.entry_price,
            "klines": [k.to_dict() for k in getattr(candidate, "_aitos_klines", ())],
            "advanced_context": advanced,
            "positioning": positioning,
            "graph_context": graph_context,
        }
        return await kernel.request_decision(DecisionContext(symbol=candidate.symbol, context=context))

    scanner_cls.scan_symbol = scan_symbol
    scanner_cls.decide_with_kernel = decide_with_kernel
    setattr(scanner_cls, _PATCHED, True)
