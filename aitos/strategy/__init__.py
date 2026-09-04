"""Universal strategy layer for AITOS."""

from .allocator import CapitalAllocator
from .builtins import (
    FundingBasisStrategy,
    HedgeStrategy,
    MarketMakingStrategy,
    OptionsVolatilityStrategy,
    RegimeRouterStrategy,
    StatisticalArbitrageStrategy,
)
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
from .engine import StrategyCycle, StrategyEngine
from .registry import StrategyRegistry

__all__ = [
    "CapitalAllocator", "CapitalRequest", "ExecutionIntent", "FundingBasisStrategy",
    "HedgeStrategy", "MarketMakingStrategy", "MarketSnapshot", "OptionsVolatilityStrategy",
    "PositionEffect", "RegimeRouterStrategy", "StatisticalArbitrageStrategy",
    "StrategyContext", "StrategyCycle", "StrategyEngine", "StrategyFamily",
    "StrategyMode", "StrategyRegistry", "StrategyResult",
]
