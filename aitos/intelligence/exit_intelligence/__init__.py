"""Exit Intelligence Engine — Phase D.

Decides HOLD / MANAGE / EXIT for an open position by evaluating whether the
original thesis is still valid and whether expected remaining edge is positive.
"""

from aitos.intelligence.exit_intelligence.engine import ExitIntelligenceEngine
from aitos.intelligence.exit_intelligence.models import (
    ExitAction,
    ExitDecision,
    ExitReason,
)

__all__ = [
    "ExitAction",
    "ExitDecision",
    "ExitIntelligenceEngine",
    "ExitReason",
]
