"""Strategy orchestration boundary between intelligence and execution/risk."""

from __future__ import annotations

from dataclasses import dataclass

from .allocator import Allocation, CapitalAllocator
from .contracts import StrategyContext, StrategyFamily, StrategyResult
from .registry import StrategyRegistry


@dataclass(frozen=True)
class StrategyCycle:
    regime: str
    results: tuple[StrategyResult, ...]
    allocations: tuple[Allocation, ...]


class StrategyEngine:
    """Run eligible strategies and produce capital-scoped results.

    This component deliberately stops before the risk gate and execution adapter.
    That preserves the AITOS invariant that no strategy can bypass shared risk,
    portfolio accounting, or venue-specific execution.
    """

    def __init__(self, registry: StrategyRegistry, allocator: CapitalAllocator) -> None:
        self.registry = registry
        self.allocator = allocator

    def run_cycle(self, context: StrategyContext) -> StrategyCycle:
        results = self.registry.evaluate(context)
        preferred = self._preferred_families(context.global_regime, results)
        if preferred is not None:
            results = tuple(r for r in results if r.family in preferred or r.family is StrategyFamily.REGIME)
        requests = [r.capital_request for r in results if r.capital_request is not None]
        allocations = self.allocator.allocate(requests)
        return StrategyCycle(context.global_regime, results, allocations)

    @staticmethod
    def _preferred_families(regime: str, results: tuple[StrategyResult, ...]) -> set[StrategyFamily] | None:
        for result in results:
            if result.family is StrategyFamily.REGIME:
                values = result.diagnostics.get("preferred_families")
                if values:
                    return {StrategyFamily(value) for value in values}
        return None
