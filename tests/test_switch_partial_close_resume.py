from __future__ import annotations

import pytest

from aitos.trading.position_switching import (
    CloseReceipt,
    PositionSwitchCoordinator,
    SQLiteSwitchJournal,
)
from tests.test_position_switching import (
    _Close,
    _Decision,
    _Gateway,
    _Lifecycle,
    _candidate,
)


@pytest.mark.asyncio
async def test_partial_close_recovery_does_not_reapply_alpha_veto(tmp_path):
    lifecycle = _Lifecycle()
    journal = SQLiteSwitchJournal(tmp_path / "partial-resume.sqlite")
    evaluations = 0
    close_calls = 0

    def evaluate(incumbent, candidate):
        nonlocal evaluations
        evaluations += 1
        return _Decision(should_switch=evaluations == 1)

    async def close_partial_then_retry(trade, client_order_id, existing_order_id):
        nonlocal close_calls
        close_calls += 1
        return CloseReceipt(
            False,
            f"close-order-{close_calls}",
            100.0,
            0.4,
            False,
        )

    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle,
        gateway=_Gateway(),
        journal=journal,
        close_and_confirm=close_partial_then_retry,
        portfolio_provider=lambda: object(),
        evaluate_pair=evaluate,
    )
    incumbent = lifecycle.open_trades[0]
    candidate = _candidate()

    assert (
        await coordinator.switch(
            incumbent=incumbent, candidate=candidate, decision=_Decision()
        )
        is None
    )
    assert journal.list_incomplete()[0].state == "close_pending"

    # The alpha evaluator now vetoes, but a partial reduction already happened.
    # The recovery path must continue reconciling the same close intent.
    assert (
        await coordinator.switch(
            incumbent=incumbent, candidate=candidate, decision=_Decision()
        )
        is None
    )
    assert evaluations == 1
    assert close_calls == 2
