"""Trade Thesis — machine-readable entry rationale for exit intelligence.

Phase G of the Market-Path / Exit-Intelligence architecture.

A TradeThesis captures *why* the position was opened so Exit Intelligence
can ask: "is the original thesis still consistent with current MarketState?"
"""

from aitos.intelligence.trade_thesis.models import (
    ConfirmationSignal,
    InvalidationCondition,
    ThesisComponent,
    ThesisHealth,
    TradeThesis,
)
from aitos.intelligence.trade_thesis.engine import TradeThesisEngine

__all__ = [
    "ConfirmationSignal",
    "InvalidationCondition",
    "ThesisComponent",
    "ThesisHealth",
    "TradeThesis",
    "TradeThesisEngine",
]
