"""Strategy discovery, enablement and deterministic dispatch."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import Strategy, StrategyContext, StrategyResult


class StrategyRegistry:
    """Registry that isolates strategy selection from execution and risk."""

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}
        self._enabled: set[str] = set()

    def register(self, strategy: Strategy, *, enabled: bool = True) -> None:
        if not strategy.strategy_id:
            raise ValueError("strategy_id is required")
        if strategy.strategy_id in self._strategies:
            raise ValueError(f"strategy already registered: {strategy.strategy_id}")
        self._strategies[strategy.strategy_id] = strategy
        if enabled:
            self._enabled.add(strategy.strategy_id)

    def enable(self, strategy_id: str) -> None:
        self._require(strategy_id)
        self._enabled.add(strategy_id)

    def disable(self, strategy_id: str) -> None:
        self._enabled.discard(strategy_id)

    def get(self, strategy_id: str) -> Strategy:
        return self._require(strategy_id)

    def enabled(self) -> tuple[Strategy, ...]:
        return tuple(
            self._strategies[strategy_id]
            for strategy_id in sorted(self._enabled)
        )

    def evaluate(self, context: StrategyContext) -> tuple[StrategyResult, ...]:
        return tuple(strategy.evaluate(context) for strategy in self.enabled())

    def _require(self, strategy_id: str) -> Strategy:
        try:
            return self._strategies[strategy_id]
        except KeyError as exc:
            raise KeyError(f"unknown strategy: {strategy_id}") from exc

    def register_many(self, strategies: Iterable[Strategy]) -> None:
        for strategy in strategies:
            self.register(strategy)
