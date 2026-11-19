"""Universal multi-asset market abstraction for AITOS.

Crypto is the first production market, but the core contracts are asset-class
agnostic so equities, FX, rates, futures, commodities and options can plug in
without changing strategy or risk code.
"""

from .adapters import ExecutionAdapter, ExecutionIntent, MarketDataAdapter
from .bridges import market_data_event_to_domain
from .calendar import MacroEvent, MarketSession, TradingCalendar
from .contracts import AssetClass, Instrument, MarketEvent, MarketEventType
from .engine import CrossMarketIntelligenceEngine, LeadLagResult
from .instrument_master import InstrumentKey, InstrumentMaster
from .portfolio import Portfolio, Position
from .risk import RiskDecision, RiskEngine
from .state import GlobalMarketState, MarketRegime, MarketStateBuilder

__all__ = [
    "AssetClass",
    "CrossMarketIntelligenceEngine",
    "ExecutionAdapter",
    "ExecutionIntent",
    "GlobalMarketState",
    "Instrument",
    "InstrumentKey",
    "InstrumentMaster",
    "LeadLagResult",
    "MacroEvent",
    "MarketDataAdapter",
    "market_data_event_to_domain",
    "MarketEvent",
    "MarketEventType",
    "MarketRegime",
    "MarketSession",
    "MarketStateBuilder",
    "Portfolio",
    "Position",
    "RiskDecision",
    "RiskEngine",
    "TradingCalendar",
]
