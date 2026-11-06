from __future__ import annotations

import pytest

from aitos.trading.lifecycle import TradeLifecycle


@pytest.mark.asyncio
async def test_lifecycle_remains_uninitialized_when_opt_in_recovery_is_misconfigured():
    lifecycle = TradeLifecycle.__new__(TradeLifecycle)
    lifecycle._initialized = False
    lifecycle._position_switch_coordinator = None

    with pytest.raises(RuntimeError, match="without a configured coordinator"):
        await lifecycle.initialize(
            {
                "recover_position_switches": True,
                "trades_hydrated": True,
                "venue_positions_reconciled": True,
            }
        )

    assert lifecycle._initialized is False


@pytest.mark.asyncio
async def test_lifecycle_initializes_normally_when_recovery_is_not_enabled():
    lifecycle = TradeLifecycle.__new__(TradeLifecycle)
    lifecycle._initialized = False
    lifecycle._position_manager = None
    lifecycle._position_switch_coordinator = None

    await lifecycle.initialize({})

    assert lifecycle._initialized is True

@pytest.mark.asyncio
async def test_lifecycle_recovery_requires_hydrated_and_reconciled_state():
    lifecycle = TradeLifecycle.__new__(TradeLifecycle)
    lifecycle._initialized = False
    lifecycle._position_switch_coordinator = object()

    with pytest.raises(RuntimeError, match="requires hydrated trades"):
        await lifecycle.initialize({"recover_position_switches": True})

    assert lifecycle._initialized is False
