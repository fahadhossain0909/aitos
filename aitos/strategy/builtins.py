"""Conservative reference strategies for the universal strategy layer.

These strategies are intent generators only. They deliberately do not call an
exchange and are safe to exercise in paper/shadow mode before activation.
"""

from __future__ import annotations

from .contracts import (
    CapitalRequest,
    ExecutionIntent,
    Strategy,
    StrategyContext,
    StrategyFamily,
    StrategyResult,
)


class FundingBasisStrategy(Strategy):
    strategy_id = "funding-basis"
    family = StrategyFamily.FUNDING_BASIS

    def __init__(self, min_funding_rate: float = 0.0005, min_edge_bps: float = 2.0) -> None:
        self.min_funding_rate = min_funding_rate
        self.min_edge_bps = min_edge_bps

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        for snap in context.snapshots.values():
            expected_edge_bps = snap.funding_rate * 10_000 - abs(snap.basis_bps)
            if snap.funding_rate < self.min_funding_rate or expected_edge_bps < self.min_edge_bps:
                continue
            notional = min(context.available_capital * 0.20, context.risk_budget)
            if notional <= 0:
                continue
            intents = (
                ExecutionIntent(snap.instrument_id, "buy", notional / snap.mid,
                                strategy_id=self.strategy_id, hedge_group=f"funding:{snap.instrument_id}",
                                rationale="positive funding net of basis estimate"),
            )
            return StrategyResult(
                self.strategy_id, self.family, intents,
                CapitalRequest(self.strategy_id, notional, notional * 0.01, expected_edge_bps / 10_000),
                diagnostics={"expected_edge_bps": expected_edge_bps},
            )
        return StrategyResult(self.strategy_id, self.family)


class StatisticalArbitrageStrategy(Strategy):
    strategy_id = "statistical-arbitrage"
    family = StrategyFamily.ARBITRAGE

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        for snap in context.snapshots.values():
            z = snap.features.get("spread_zscore", 0.0)
            if abs(z) < 2.0:
                continue
            side = "sell" if z > 0 else "buy"
            notional = min(context.available_capital * 0.10, context.risk_budget)
            if notional <= 0:
                continue
            return StrategyResult(
                self.strategy_id,
                self.family,
                (ExecutionIntent(snap.instrument_id, side, notional / snap.mid,
                                 strategy_id=self.strategy_id,
                                 rationale=f"spread z-score={z:.2f}"),),
                CapitalRequest(self.strategy_id, notional, notional * 0.02, abs(z) / 100),
            )
        return StrategyResult(self.strategy_id, self.family)


class MarketMakingStrategy(Strategy):
    strategy_id = "market-making"
    family = StrategyFamily.MARKET_MAKING

    def __init__(self, min_spread_bps: float = 4.0, max_inventory_ratio: float = 0.25) -> None:
        self.min_spread_bps = min_spread_bps
        self.max_inventory_ratio = max_inventory_ratio

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        for snap in context.snapshots.values():
            if snap.spread_bps < self.min_spread_bps or snap.liquidity_score <= 0:
                continue
            inventory = context.positions.get(snap.instrument_id, 0.0) * snap.mid
            cap = context.available_capital * self.max_inventory_ratio
            if abs(inventory) >= cap:
                continue
            quote_notional = min(context.available_capital * 0.02, max(0.0, cap - abs(inventory)))
            if quote_notional <= 0:
                continue
            buy_qty = quote_notional / snap.mid
            sell_qty = quote_notional / snap.mid
            half_spread = snap.mid * snap.spread_bps / 20000
            return StrategyResult(
                self.strategy_id,
                self.family,
                (
                    ExecutionIntent(snap.instrument_id, "buy", buy_qty, "limit", snap.mid - half_spread,
                                    strategy_id=self.strategy_id, rationale="liquid spread capture"),
                    ExecutionIntent(snap.instrument_id, "sell", sell_qty, "limit", snap.mid + half_spread,
                                    strategy_id=self.strategy_id, rationale="liquid spread capture"),
                ),
                CapitalRequest(self.strategy_id, quote_notional, quote_notional * 0.01, snap.spread_bps / 10_000),
                diagnostics={"inventory_notional": inventory},
            )
        return StrategyResult(self.strategy_id, self.family)


class RegimeRouterStrategy(Strategy):
    """Selects strategy families from global regime without placing orders."""

    strategy_id = "regime-router"
    family = StrategyFamily.REGIME

    ROUTES = {
        "bull": (StrategyFamily.DIRECTIONAL, StrategyFamily.ARBITRAGE),
        "bear": (StrategyFamily.DIRECTIONAL, StrategyFamily.HEDGING),
        "sideways": (StrategyFamily.MARKET_MAKING, StrategyFamily.ARBITRAGE, StrategyFamily.FUNDING_BASIS),
        "high_volatility": (StrategyFamily.HEDGING, StrategyFamily.ARBITRAGE),
        "low_volatility": (StrategyFamily.MARKET_MAKING, StrategyFamily.FUNDING_BASIS),
    }

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        families = self.ROUTES.get(context.global_regime, (StrategyFamily.DIRECTIONAL,))
        return StrategyResult(
            self.strategy_id,
            self.family,
            diagnostics={"regime": context.global_regime, "preferred_families": tuple(f.value for f in families)},
        )
