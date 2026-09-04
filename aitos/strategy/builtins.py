"""Conservative reference strategies for the universal strategy layer."""

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

    def __init__(
        self, min_funding_rate: float = 0.0005, min_edge_bps: float = 2.0
    ) -> None:
        self.min_funding_rate = min_funding_rate
        self.min_edge_bps = min_edge_bps

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        for snap in context.snapshots.values():
            edge_bps = snap.funding_rate * 10_000 - abs(snap.basis_bps)
            if (
                snap.funding_rate < self.min_funding_rate
                or edge_bps < self.min_edge_bps
            ):
                continue
            notional = min(context.available_capital * 0.20, context.risk_budget)
            if notional <= 0:
                continue
            return StrategyResult(
                self.strategy_id,
                self.family,
                (
                    ExecutionIntent(
                        snap.instrument_id,
                        "buy",
                        notional / snap.mid,
                        strategy_id=self.strategy_id,
                        hedge_group=f"funding:{snap.instrument_id}",
                        rationale="positive funding net of basis estimate",
                    ),
                ),
                CapitalRequest(
                    self.strategy_id, notional, notional * 0.01, edge_bps / 10_000
                ),
                diagnostics={"expected_edge_bps": edge_bps},
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
            notional = min(context.available_capital * 0.10, context.risk_budget)
            if notional <= 0:
                continue
            side = "sell" if z > 0 else "buy"
            return StrategyResult(
                self.strategy_id,
                self.family,
                (
                    ExecutionIntent(
                        snap.instrument_id,
                        side,
                        notional / snap.mid,
                        strategy_id=self.strategy_id,
                        rationale=f"spread z-score={z:.2f}",
                    ),
                ),
                CapitalRequest(
                    self.strategy_id, notional, notional * 0.02, abs(z) / 100
                ),
            )
        return StrategyResult(self.strategy_id, self.family)


class MarketMakingStrategy(Strategy):
    strategy_id = "market-making"
    family = StrategyFamily.MARKET_MAKING

    def __init__(
        self, min_spread_bps: float = 4.0, max_inventory_ratio: float = 0.25
    ) -> None:
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
            quote = min(context.available_capital * 0.02, cap - abs(inventory))
            if quote <= 0:
                continue
            half = snap.mid * snap.spread_bps / 20_000
            qty = quote / snap.mid
            return StrategyResult(
                self.strategy_id,
                self.family,
                (
                    ExecutionIntent(
                        snap.instrument_id,
                        "buy",
                        qty,
                        "limit",
                        snap.mid - half,
                        strategy_id=self.strategy_id,
                        rationale="liquid spread capture",
                    ),
                    ExecutionIntent(
                        snap.instrument_id,
                        "sell",
                        qty,
                        "limit",
                        snap.mid + half,
                        strategy_id=self.strategy_id,
                        rationale="liquid spread capture",
                    ),
                ),
                CapitalRequest(
                    self.strategy_id, quote, quote * 0.01, snap.spread_bps / 10_000
                ),
                diagnostics={"inventory_notional": inventory},
            )
        return StrategyResult(self.strategy_id, self.family)


class HedgeStrategy(Strategy):
    """Reduce portfolio delta through the configured hedge instrument."""

    strategy_id = "delta-hedge"
    family = StrategyFamily.HEDGING

    def __init__(self, hedge_instrument: str, max_delta: float = 0.05) -> None:
        self.hedge_instrument = hedge_instrument
        self.max_delta = max_delta

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        if abs(context.portfolio_delta) <= self.max_delta:
            return StrategyResult(self.strategy_id, self.family)
        snap = context.snapshots.get(self.hedge_instrument)
        if snap is None or snap.mid <= 0:
            return StrategyResult(
                self.strategy_id,
                self.family,
                diagnostics={"reason": "hedge_market_unavailable"},
            )
        side = "sell" if context.portfolio_delta > 0 else "buy"
        qty = abs(context.portfolio_delta)
        return StrategyResult(
            self.strategy_id,
            self.family,
            (
                ExecutionIntent(
                    self.hedge_instrument,
                    side,
                    qty,
                    reduce_only=False,
                    strategy_id=self.strategy_id,
                    hedge_group="portfolio-delta",
                    rationale="portfolio delta hedge",
                ),
            ),
            diagnostics={"portfolio_delta": context.portfolio_delta},
        )


class OptionsVolatilityStrategy(Strategy):
    """Reference volatility strategy driven by normalized IV/realized-vol features."""

    strategy_id = "options-volatility"
    family = StrategyFamily.OPTIONS

    def __init__(self, min_iv_edge: float = 0.10) -> None:
        self.min_iv_edge = min_iv_edge

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        for snap in context.snapshots.values():
            iv_edge = snap.features.get("iv_edge", 0.0)
            if iv_edge <= self.min_iv_edge:
                continue
            notional = min(context.available_capital * 0.05, context.risk_budget)
            if notional <= 0:
                continue
            return StrategyResult(
                self.strategy_id,
                self.family,
                (
                    ExecutionIntent(
                        snap.instrument_id,
                        "sell",
                        notional / snap.mid,
                        strategy_id=self.strategy_id,
                        hedge_group=f"option-vol:{snap.instrument_id}",
                        rationale="implied-volatility edge",
                    ),
                ),
                CapitalRequest(self.strategy_id, notional, notional * 0.03, iv_edge),
                diagnostics={"iv_edge": iv_edge},
            )
        return StrategyResult(self.strategy_id, self.family)


class RegimeRouterStrategy(Strategy):
    """Select strategy families from the global regime without placing orders."""

    strategy_id = "regime-router"
    family = StrategyFamily.REGIME

    ROUTES = {
        "bull": (StrategyFamily.DIRECTIONAL, StrategyFamily.ARBITRAGE),
        "bear": (StrategyFamily.DIRECTIONAL, StrategyFamily.HEDGING),
        "sideways": (
            StrategyFamily.MARKET_MAKING,
            StrategyFamily.ARBITRAGE,
            StrategyFamily.FUNDING_BASIS,
        ),
        "high_volatility": (StrategyFamily.HEDGING, StrategyFamily.ARBITRAGE),
        "low_volatility": (StrategyFamily.MARKET_MAKING, StrategyFamily.FUNDING_BASIS),
    }

    def evaluate(self, context: StrategyContext) -> StrategyResult:
        families = self.ROUTES.get(context.global_regime, (StrategyFamily.DIRECTIONAL,))
        return StrategyResult(
            self.strategy_id,
            self.family,
            diagnostics={
                "regime": context.global_regime,
                "preferred_families": tuple(f.value for f in families),
            },
        )
