"""Opt-in Binance Spot market integration.

The package is deliberately isolated from Futures execution. Importing it never
enables trading; callers must explicitly construct an executor with credentials
and an explicit environment selection.
"""
from aitos.spot.filters import SpotSymbolFilters, parse_spot_exchange_info
from aitos.spot.models import (
    SpotBalance, SpotFill, SpotInstrument, SpotOrder, SpotOrderRequest,
    SpotOrderSide, SpotOrderStatus, SpotOrderType,
)
from aitos.spot.inventory import InventoryLedger, InventoryPosition
from aitos.spot.scanner import SpotCandidate, SpotOpportunityScanner

__all__ = [
    "SpotBalance", "SpotCandidate", "SpotFill", "SpotInstrument",
    "SpotOrder", "SpotOrderRequest", "SpotOrderSide", "SpotOrderStatus",
    "SpotOrderType", "SpotSymbolFilters", "InventoryLedger",
    "InventoryPosition", "SpotOpportunityScanner", "parse_spot_exchange_info",
]
