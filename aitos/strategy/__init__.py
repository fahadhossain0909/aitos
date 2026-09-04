"""Universal strategy layer for AITOS.

Strategies emit execution intents; shared portfolio, risk and execution layers
remain strategy-agnostic.  The package is deliberately independent of any
single venue or asset class.
"""

from .contracts import (
    CapitalRequest,
    ExecutionIntent,
    MarketSnapshot,
    PositionEffect,
    StrategyContext,
    StrategyFamily,
    StrategyMode,
    StrategyResult,
)
from .registry import StrategyRegistry
from .allocator import CapitalAllocator
from .builtins import (
    FundingBasisStrategy,
    MarketMakingStrategy,
    RegimeRouterStrategy,
    StatisticalArbitrageStrategy,
)

__all__ = [
    "CapitalAllocator",
    "CapitalRequest",
    "ExecutionIntent",
    "FundingBasisStrategy",
    "MarketMakingStrategy",
    "MarketSnapshot",
    "PositionEffect",
    "RegimeRouterStrategy",
    "StatisticalArbitrageStrategy",
    "StrategyContext",
    "StrategyFamily",
    "StrategyMode",
    "StrategyRegistry",
    "StrategyResult",
]
