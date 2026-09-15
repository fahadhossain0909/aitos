from .lifecycle import TradeLifecycle
from .lifecycle_safety import install_lifecycle_event_safety
from .position_manager import PositionAction, PositionManager
from .reconciliation import ReconciliationScheduler

install_lifecycle_event_safety(TradeLifecycle)

# These two intelligence-layer guards patch TradeLifecycle itself (capital
# gate on submit_opportunity; position-monitor wiring + telemetry on
# submit_opportunity/update_price). They used to self-install at their own
# module's import time (aitos.intelligence.capital_runtime /
# aitos.intelligence.position_runtime), which closed a real circular import:
# aitos.trading -> aitos.kernel -> aitos.intelligence ->
# [capital_runtime/position_runtime] -> aitos.trading.lifecycle, reached
# before TradeLifecycle existed yet. That only "worked" for entry points
# that happened to import aitos.app or aitos.intelligence before aitos.trading
# -- importing aitos.trading, aitos.kernel, or aitos.trading.lifecycle
# directly, as the first import in a process, raised ImportError. Triggering
# them here instead is safe by construction: by this point TradeLifecycle
# above has already fully imported, which means aitos.kernel and
# aitos.intelligence (transitive dependencies of aitos.trading.lifecycle)
# are already fully loaded too -- so importing from them below is just a
# sys.modules lookup, never a fresh import.
from aitos.intelligence.capital_runtime import install_capital_guard
from aitos.intelligence.position_runtime import install_lifecycle_guards

install_capital_guard()
install_lifecycle_guards()

__all__ = [
    "PositionAction",
    "PositionManager",
    "ReconciliationScheduler",
    "TradeLifecycle",
]
