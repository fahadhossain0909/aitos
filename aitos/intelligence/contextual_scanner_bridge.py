"""Bridge the existing opportunity scanner into contextual intelligence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aitos.intelligence.advanced_context import (
    AdvancedMarketContext,
)
from aitos.intelligence.contextual_layers import PositioningContext
from aitos.intelligence.graph_context import retrieve_graph_context

_PATCHED = "_aitos_contextual_bridge_patched"
_ORIGINAL_SCAN = "_aitos_original_scan_symbol"
_ORIGINAL_DECIDE = "_aitos_original_decide_with_kernel"



def _live_liquidity_flow_evidence(store: Any, symbol: str):
    """Build freshness-gated flow evidence from the shared live-state store."""
    from aitos.intelligence.liquidity_intelligence import OrderFlowEvidence

    if store is None:
        return OrderFlowEvidence()
    try:
        snapshot = store.snapshot_model(symbol)
    except (AttributeError, KeyError, TypeError, ValueError):
        return OrderFlowEvidence()
    flow = getattr(snapshot, "order_flow", None)
    book = getattr(snapshot, "order_book", None)
    if flow is None or book is None:
        return OrderFlowEvidence()

    now = datetime.now(timezone.utc)

    def is_fresh(timestamp: Any) -> bool:
        if timestamp is None:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = (now - timestamp.astimezone(timezone.utc)).total_seconds()
        return 0.0 <= age <= 15.0

    flow_fresh = is_fresh(getattr(flow, "timestamp", None))
    book_fresh = is_fresh(getattr(book, "timestamp", None))
    trade_count = max(0, int(getattr(flow, "trade_count", 0)))
    fresh = flow_fresh and book_fresh and trade_count >= 5

    bids = tuple(getattr(book, "bids", ()) or ())[:10]
    asks = tuple(getattr(book, "asks", ()) or ())[:10]
    bid_depth = sum(max(0.0, float(level[1])) for level in bids)
    ask_depth = sum(max(0.0, float(level[1])) for level in asks)
    total_depth = bid_depth + ask_depth
    ofi_score = (
        max(0.0, min(10.0, 5.0 + 5.0 * (bid_depth - ask_depth) / total_depth))
        if total_depth > 0
        else 5.0
    )
    buy_ratio = max(0.0, min(1.0, float(getattr(flow, "buy_ratio", 0.5))))
    cvd_proxy = max(0.0, min(10.0, buy_ratio * 10.0))
    events = tuple(getattr(snapshot, "liquidity_events", ()) or ())
    bid_replenishment = min(
        10.0,
        sum(
            max(0.0, float(event.score))
            for event in events
            if event.side == "bid" and event.kind == "stacking"
        ),
    )
    ask_replenishment = min(
        10.0,
        sum(
            max(0.0, float(event.score))
            for event in events
            if event.side == "ask" and event.kind == "stacking"
        ),
    )
    return OrderFlowEvidence(
        cvd_score=cvd_proxy,
        ofi_score=ofi_score,
        fresh=fresh,
        confidence=min(1.0, trade_count / 20.0) if fresh else 0.0,
        bid_replenishment=bid_replenishment,
        ask_replenishment=ask_replenishment,
        aggressive_buy_ratio=buy_ratio,
    )


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
        # Deep history is fetched only after global Top-2 promotion.
        # Never fan out historical REST calls for every candidate symbol.
        return candidate

    async def decide_with_kernel(self: Any, candidate: Any, kernel: Any):
        from aitos.kernel.ai_kernel import DecisionContext

        context_scores = dict(candidate.component_scores)
        advanced = getattr(candidate, "_aitos_advanced_context", None)
        if isinstance(advanced, AdvancedMarketContext):
            if advanced.volume_profile is not None:
                context_scores["volume_profile"] = round(
                    advanced.volume_profile.price_location * 10.0, 4
                )
            context_scores["price_imbalance"] = round(
                5.0 + advanced.imbalance.displacement_score * 5.0, 4
            )
            context_scores["structural_symmetry"] = round(
                5.0
                + (advanced.symmetry.similarity * 5.0 if advanced.symmetry else 0.0),
                4,
            )
            context_scores["forced_flow"] = round(
                5.0 + min(5.0, advanced.forced_flow_score / 2.0), 4
            )

        availability = dict(candidate.component_availability)
        if isinstance(advanced, AdvancedMarketContext):
            availability.update(
                {
                    "volume_profile": advanced.volume_profile is not None,
                    "price_imbalance": bool(advanced.imbalance.zones),
                    "structural_symmetry": advanced.symmetry is not None,
                    "forced_flow": True,
                }
            )

        positioning: PositioningContext | None = getattr(candidate, "positioning", None)
        regime = getattr(candidate.regime, "value", candidate.regime)
        graph_context = await retrieve_graph_context(
            symbol=candidate.symbol,
            regime=str(regime),
            direction=candidate.direction.value,
            strategy_id=str(getattr(candidate, "strategy_id", "") or ""),
            model_id=str(getattr(candidate, "model_id", "") or ""),
            limit=12,
        )
        context_scores["graph_historical_support"] = graph_context["score"]
        availability["graph_historical_support"] = graph_context["available"]
        # Evidence-only Liquidity Intelligence. This enriches the Kernel context
        # and attribution trail but deliberately does not alter ranking or gates.
        liquidity_payload = None
        candidate_klines = getattr(candidate, "_aitos_klines", ())
        if candidate_klines:
            try:
                from aitos.intelligence.liquidity_intelligence import (
                    OrderFlowEvidence,
                    analyze_liquidity,
                )

                state_store = getattr(self, "_aitos_liquidity_state_store", None)
                flow_evidence = _live_liquidity_flow_evidence(
                    state_store, candidate.symbol
                )
                if not flow_evidence.fresh:
                    flow_score = float(context_scores.get("order_flow_bias", 5.0))
                    flow_evidence = OrderFlowEvidence(
                        cvd_score=max(0.0, min(10.0, flow_score)),
                        ofi_score=max(
                            0.0,
                            min(
                                10.0,
                                float(
                                    context_scores.get("order_book_flow_imbalance", 5.0)
                                ),
                            ),
                        ),
                    )
                liquidity_analysis = analyze_liquidity(
                    candidate.symbol,
                    str(getattr(candidate_klines[-1], "timeframe", "unknown")),
                    candidate_klines,
                    direction=candidate.direction.value,
                    flow=flow_evidence,
                )
                liquidity_payload = liquidity_analysis.to_dict()
                update_levels = getattr(state_store, "update_liquidity_levels", None)
                if callable(update_levels):
                    try:
                        update_levels(candidate.symbol, list(liquidity_analysis.levels))
                    except (AttributeError, TypeError, ValueError):
                        pass
                record = getattr(self, "_record_funnel", None)
                if callable(record):
                    await record(
                        symbol=candidate.symbol,
                        side=candidate.direction.value,
                        stage="liquidity_intelligence_shadow",
                        accepted=True,
                        reason="evidence_only_no_decision_impact",
                        details={
                            "candidate_id": getattr(candidate, "candidate_id", None),
                            "liquidity_level_count": len(liquidity_analysis.levels),
                            "sweep_count": len(liquidity_analysis.sweeps),
                            "risk_score": liquidity_analysis.risk.score,
                            "risk_action": liquidity_analysis.risk.action.value,
                            "latest_sweep_state": (
                                liquidity_analysis.latest_classification.state.value
                                if liquidity_analysis.latest_classification
                                else None
                            ),
                            "decision_impact": False,
                        },
                    )
            except (AttributeError, TypeError, ValueError):
                liquidity_payload = {
                    "available": False,
                    "reason": "liquidity_analysis_invalid_or_unavailable",
                    "decision_impact": False,
                }

        context = {
            "direction": candidate.direction.value,
            "component_scores": context_scores,
            "component_availability": availability,
            "regime": candidate.regime,
            "entry_price": candidate.entry_price,
            "klines": [k.to_dict() for k in getattr(candidate, "_aitos_klines", ())],
            "advanced_context": advanced,
            "candidate_id": getattr(candidate, "_aitos_candidate_id", None),
            "historical_intelligence": getattr(
                candidate, "_aitos_historical_context", None
            ),
            "historical_evidence_id": getattr(
                candidate, "_aitos_historical_evidence_id", None
            ),
            "positioning": positioning,
            "graph_context": graph_context,
            "liquidity_intelligence": liquidity_payload,
            "liquidity_intelligence_mode": "shadow",
        }
        return await kernel.request_decision(
            DecisionContext(symbol=candidate.symbol, context=context)
        )

    scanner_cls.scan_symbol = scan_symbol
    scanner_cls.decide_with_kernel = decide_with_kernel
    setattr(scanner_cls, _PATCHED, True)
