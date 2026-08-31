"""Structural Risk Engine — Phase C.

Answers: “At what price is the original trade thesis objectively invalid?”
That level becomes the structural stop-loss. Position sizing is then derived
from risk-budget ÷ structural distance.
"""

from aitos.intelligence.structural_risk.engine import StructuralRiskEngine
from aitos.intelligence.structural_risk.models import StructuralStop

__all__ = ["StructuralRiskEngine", "StructuralStop"]
