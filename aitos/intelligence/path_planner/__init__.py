"""Market Path Planner — Phase B of the Exit-Intelligence architecture.

Given a MarketState and optional volume-profile / liquidity / structure
inputs, produces a ranked set of probable price destinations.
"""

from aitos.intelligence.path_planner.models import PathDestination, PathPlan
from aitos.intelligence.path_planner.planner import MarketPathPlanner

__all__ = ["MarketPathPlanner", "PathPlan", "PathDestination"]
