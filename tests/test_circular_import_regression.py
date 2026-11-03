"""Regression test for a real circular import between aitos.trading,
aitos.kernel, and aitos.intelligence.

aitos.trading.lifecycle needs aitos.kernel.ai_kernel, which needs
aitos.intelligence.contextual_decision (pulling in the whole aitos.intelligence
package __init__), which used to eagerly install cross-package guards
(aitos.intelligence.capital_runtime.install_capital_guard(),
aitos.intelligence.position_runtime's lifecycle/position-monitor installers)
that themselves needed aitos.trading.lifecycle.TradeLifecycle /
aitos.trading.position_manager.PositionManager to already exist -- closing
the loop before those classes were defined yet.

This only "worked" because aitos.app and aitos.intelligence happened to
establish a safe order first. Importing aitos.trading, aitos.kernel, or
aitos.trading.lifecycle directly -- as the very first import in a fresh
process -- raised ImportError/AttributeError. Each entry point below is
exercised in its own fresh interpreter (sys.modules caching would otherwise
hide the bug after the first import in-process), matching exactly how this
was originally diagnosed.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

ENTRY_POINTS = [
    "aitos.app",
    "aitos.intelligence",
    "aitos.kernel",
    "aitos.trading",
    "aitos.trading.lifecycle",
    "aitos.trading.position_manager",
    "aitos.intelligence.capital_runtime",
    "aitos.intelligence.position_runtime",
]


@pytest.mark.parametrize("module_name", ENTRY_POINTS)
def test_module_imports_cleanly_as_the_first_import_in_a_fresh_process(
    module_name: str,
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"import {module_name} failed as the first import in a fresh "
        f"process:\n{result.stderr}"
    )


def test_capital_guard_and_position_monitor_actually_get_installed() -> None:
    """Moving the install calls doesn't just avoid the ImportError -- the
    guards still have to actually end up installed on TradeLifecycle /
    PositionManager, whichever entry point triggers the import first."""
    script = (
        "import aitos.trading.lifecycle as lc\n"
        "import aitos.trading.position_manager as pm\n"
        "assert hasattr(lc.TradeLifecycle, '_aitos_capital_original_submit_opportunity')\n"
        "assert hasattr(lc.TradeLifecycle, '_aitos_position_runtime_installed')\n"
        "assert hasattr(lc.TradeLifecycle, '_aitos_lifecycle_telemetry_installed')\n"
        "assert hasattr(pm.PositionManager, '_aitos_tiered_monitor_installed')\n"
        "print('all guards installed')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "all guards installed" in result.stdout
