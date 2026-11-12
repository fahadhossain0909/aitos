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


async def _persist_liquidity_evidence(
    scanner: Any,
    candidate: Any,
    analysis: Any,
    amd_state: dict[str, Any] | None = None,
) -> str | None:
    """Persist a deterministic, candidate-linked liquidity evidence snapshot."""
    service = getattr(scanner, "_aitos_historical_intelligence", None)
    persist = getattr(service, "persist", None)
    candidate_id = getattr(candidate, "_aitos_candidate_id", None)
    if not callable(persist) or not candidate_id:
        return None
    rows = getattr(candidate, "_aitos_klines", ())
    if not rows:
        return None
    latest = rows[-1]
    captured_at = getattr(latest, "close_time", None) or getattr(
        latest, "timestamp", None
    )
    if hasattr(captured_at, "isoformat"):
        captured_at = captured_at.isoformat()
    if not captured_at:
        return None
    timeframe = str(
        getattr(latest, "timeframe", getattr(scanner, "_timeframe", "unknown"))
    )
    evidence = {
        "schema_version": 2 if amd_state is not None else 1,
        "symbol": str(candidate.symbol).upper(),
        "timeframe": timeframe,
        "captured_at": str(captured_at),
        "liquidity_intelligence": analysis.to_dict(),
    }
    if amd_state is not None:
        evidence["amd_intelligence"] = amd_state
    try:
        return await persist(str(candidate_id), evidence)
    except Exception:
        return None


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
                flow_evidence_source = (
                    "legacy_live_snapshot_proxy"
                    if flow_evidence.fresh
                    else "unavailable"
                )
                event_flow_diagnostics = {}
                live_cache = getattr(self, "_live_cache", None)
                live_state = (
                    live_cache.snapshot(candidate.symbol)
                    if live_cache is not None
                    else None
                )
                if live_state is not None:
                    from aitos.intelligence.amd_research import (
                        build_live_event_order_flow,
                    )

                    event_flow = build_live_event_order_flow(
                        tuple(getattr(live_state, "trades", ()) or ()),
                        tuple(getattr(live_state, "depth_event_history", ()) or ()),
                        as_of_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                        max_staleness_ms=int(
                            max(
                                500.0,
                                float(getattr(self, "_live_state_stale_seconds", 5.0))
                                * 1000.0,
                            )
                        ),
                        history_gap_at_ms=getattr(
                            live_state, "depth_history_gap_at_ms", None
                        ),
                    )
                    event_flow_diagnostics = {
                        "trade_count": event_flow.trade_count,
                        "depth_update_count": event_flow.depth_update_count,
                        "duplicate_trade_count": event_flow.duplicate_trade_count,
                        "unclassified_trade_count": (
                            event_flow.unclassified_trade_count
                        ),
                        "latest_event_ms": event_flow.latest_event_ms,
                        "diagnostics": list(event_flow.diagnostics),
                        "fresh": event_flow.evidence.fresh,
                    }
                    if event_flow.evidence.fresh:
                        flow_evidence = event_flow.evidence
                        flow_evidence_source = "sequenced_trade_and_depth_events"
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
                from aitos.intelligence.amd_intelligence import (
                    analyze_amd,
                    analyze_amd_timeframe_stack,
                    load_amd_checkpoint,
                    save_amd_checkpoint,
                )

                candidate_timeframe = str(
                    getattr(
                        candidate_klines[-1],
                        "timeframe",
                        getattr(self, "_timeframe", "unknown"),
                    )
                ).lower()
                timeframe_candles = dict(
                    getattr(candidate, "_aitos_mtf_candles", {}) or {}
                )
                timeframe_candles[candidate_timeframe] = tuple(candidate_klines)
                checkpoint_redis = getattr(
                    getattr(self, "_aitos_historical_intelligence", None),
                    "redis",
                    None,
                )
                amd_states = {}
                checkpoint_results = {}
                for timeframe, frame_candles in timeframe_candles.items():
                    if not frame_candles:
                        continue
                    if timeframe == candidate_timeframe:
                        frame_liquidity = liquidity_analysis
                        frame_flow = flow_evidence
                    else:
                        frame_flow = OrderFlowEvidence()
                        frame_liquidity = analyze_liquidity(
                            candidate.symbol,
                            timeframe,
                            frame_candles,
                            flow=frame_flow,
                        )
                    previous_state = await load_amd_checkpoint(
                        checkpoint_redis, candidate.symbol, timeframe
                    )
                    frame_state = analyze_amd(
                        candidate.symbol,
                        timeframe,
                        frame_candles,
                        liquidity=frame_liquidity,
                        flow=frame_flow,
                        previous_state=previous_state,
                    )
                    amd_states[timeframe] = frame_state
                    checkpoint_results[timeframe] = await save_amd_checkpoint(
                        checkpoint_redis, frame_state
                    )
                amd_state = amd_states[candidate_timeframe]
                amd_stack = analyze_amd_timeframe_stack(amd_states)
                amd_payload = amd_state.to_dict()
                amd_payload["timeframe_stack"] = amd_stack.to_dict()
                amd_payload["order_flow_source"] = flow_evidence_source
                amd_payload["event_flow_diagnostics"] = event_flow_diagnostics
                amd_payload["checkpoint_persisted"] = checkpoint_results.get(
                    candidate_timeframe, False
                )
                amd_payload["checkpoint_store_available"] = checkpoint_redis is not None
                amd_payload["timeframe_checkpoint_persistence"] = checkpoint_results
                amd_payload["decision_impact"] = False
                liquidity_payload["amd_intelligence"] = amd_payload
                object.__setattr__(candidate, "_aitos_amd_state", amd_payload)
                object.__setattr__(
                    candidate,
                    "_aitos_amd_summary",
                    {
                        "phase": amd_state.phase.value,
                        "direction": amd_state.direction,
                        "state_id": amd_state.state_id,
                        "confidence": amd_state.confidence,
                        "evidence_coverage": amd_state.evidence_coverage,
                        "timeframe_stack": amd_stack.to_dict(),
                        "decision_impact": False,
                    },
                )
                liquidity_evidence_id = await _persist_liquidity_evidence(
                    self, candidate, liquidity_analysis, amd_payload
                )
                liquidity_summary = {
                    "schema_version": 1,
                    "mode": "shadow",
                    "evidence_id": liquidity_evidence_id,
                    "level_ids": [
                        level.level_id for level in liquidity_analysis.levels
                    ],
                    "sweep_event_ids": [
                        event.event_id for event in liquidity_analysis.sweeps[-20:]
                    ],
                    "latest_classification": (
                        liquidity_analysis.latest_classification.state.value
                        if liquidity_analysis.latest_classification
                        else None
                    ),
                    "risk_score": liquidity_analysis.risk.score,
                    "risk_action": liquidity_analysis.risk.action.value,
                    "decision_impact": False,
                }
                object.__setattr__(
                    candidate, "_aitos_liquidity_summary", liquidity_summary
                )
                object.__setattr__(
                    candidate, "_aitos_liquidity_evidence_id", liquidity_evidence_id
                )
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
                            "liquidity_evidence_id": liquidity_evidence_id,
                            "amd_phase": amd_state.phase.value,
                            "amd_direction": amd_state.direction,
                            "amd_state_id": amd_state.state_id,
                            "amd_timeframe_direction": amd_stack.direction,
                            "amd_timeframe_stack_complete": amd_stack.stack_complete,
                            "amd_missing_timeframes": list(
                                amd_stack.missing_timeframes
                            ),
                            "amd_decision_impact": False,
                            "amd_order_flow_source": flow_evidence_source,
                            "amd_event_flow_fresh": bool(
                                event_flow_diagnostics.get("fresh", False)
                            ),
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
            "amd_intelligence": (
                liquidity_payload.get("amd_intelligence")
                if isinstance(liquidity_payload, dict)
                else None
            ),
            "amd_intelligence_mode": "shadow",
            "liquidity_intelligence_mode": "shadow",
        }
        return await kernel.request_decision(
            DecisionContext(symbol=candidate.symbol, context=context)
        )

    scanner_cls.scan_symbol = scan_symbol
    scanner_cls.decide_with_kernel = decide_with_kernel
    setattr(scanner_cls, _PATCHED, True)
