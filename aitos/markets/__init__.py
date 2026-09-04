"""Universal multi-asset market abstraction for AITOS.

Crypto is the first production market, but the core contracts are asset-class
agnostic so equities, FX, rates, futures, commodities and options can plug in
without changing strategy or risk code.
"""

from .contracts import AssetClass, Instrument, MarketEvent, MarketEventType
from .engine import CrossMarketIntelligenceEngine
from .portfolio import Portfolio, Position
from .risk import RiskDecision, RiskEngine
from .state import GlobalMarketState, MarketRegime

__all__ = [
    "AssetClass",
    "CrossMarketIntelligenceEngine",
    "GlobalMarketState",
    "Instrument",
    "MarketEvent",
    "MarketEventType",
    "MarketRegime",
    "Portfolio",
    "Position",
    "RiskDecision",
    "RiskEngine",
]
