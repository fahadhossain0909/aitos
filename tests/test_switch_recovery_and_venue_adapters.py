from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aitos.execution.binance_cost_adapter import BinanceUsdMExecutionCostAdapter
from aitos.execution.cost_intelligence import VenueExecutionCostRegistry
from aitos.trading.switch_recovery import PositionSwitchRecoveryWorker
from tests.test_position_switching import (
    _Close,
    _Decision,
    _Gateway,
    _Lifecycle,
    _candidate,
)
from aitos.trading.position_switching import (
    PositionSwitchCoordinator,
    SQLiteSwitchJournal,
)


@pytest.mark.asyncio
async def test_binance_adapter_builds_fee_funding_slippage_and_liquidation_quote():
    adapter = BinanceUsdMExecutionCostAdapter(
        taker_fee_rate_provider=lambda **_: {
            "entry_taker_rate": 0.0005,
            "exit_taker_rate": 0.0005,
        },
        funding_rate_provider=lambda **_: {
            "rate": -0.0001,
            "interval_seconds": 8 * 3600,
        },
        depth_slippage_bps_provider=lambda **_: {"round_trip_bps": 2.5},
        liquidation_distance_provider=lambda **_: {"distance_pct": 5.0},
    )
    registry = VenueExecutionCostRegistry(max_quote_age_seconds=30)
    registry.register("binance", adapter)
    quote = await registry.quote(
        venue="binance",
        symbol="BTCUSDT",
        side="LONG",
        notional_usd=1000,
        leverage=3,
        holding_horizon_seconds=8 * 3600,
    )
    assert quote.round_trip_fee_bps == pytest.approx(10.0)
    assert quote.funding_cost_bps == pytest.approx(-1.0)
    assert quote.expected_slippage_bps == pytest.approx(2.5)
    assert quote.liquidation_distance_pct == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_pending_candidate_is_reconstructed_and_resumed_after_restart(tmp_path):
    lifecycle = _Lifecycle()
    gateway = _Gateway()
    journal = SQLiteSwitchJournal(tmp_path / "durable.sqlite")
    close_calls = 0

    async def close_once_then_confirm(trade, client_order_id, existing_order_id):
        nonlocal close_calls
        close_calls += 1
        from aitos.trading.position_switching import CloseReceipt
        if close_calls == 1:
            return CloseReceipt(False, "exchange-close", 0.0, 0.4, False)
        return CloseReceipt(True, "exchange-close", 101.0, trade.quantity, True)

    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle,
        gateway=gateway,
        journal=journal,
        close_and_confirm=close_once_then_confirm,
        portfolio_provider=lambda: object(),
        evaluate_pair=lambda incumbent, candidate: _Decision(),
    )
    incumbent = lifecycle.open_trades[0]
    candidate = _candidate()
    first = await coordinator.switch(
        incumbent=incumbent, candidate=candidate, decision=_Decision()
    )
    assert first is None
    intent = journal.list_incomplete()[0]
    assert intent.candidate_payload["opportunity_id"] == candidate.opportunity_id
    assert intent.candidate_payload["symbol"] == candidate.symbol

    # Simulate a fresh application process: same durable journal, hydrated
    # lifecycle state and a new coordinator/worker instance.
    recovered = PositionSwitchCoordinator(
        lifecycle=lifecycle, gateway=gateway, journal=journal,
        close_and_confirm=close_once_then_confirm,
        portfolio_provider=lambda: object(),
        evaluate_pair=lambda incumbent, candidate: _Decision(),
    )
    report = await PositionSwitchRecoveryWorker(recovered).recover_pending()
    assert report.scanned == 1
    assert report.resumed == 1
    assert report.manual_reconciliation == 0
    assert lifecycle.open_calls == 1
    assert journal.list_incomplete() == []


@pytest.mark.asyncio
async def test_recovery_fails_closed_when_candidate_payload_missing(tmp_path):
    from aitos.trading.position_switching import SwitchIntent
    lifecycle = _Lifecycle()
    journal = SQLiteSwitchJournal(tmp_path / "missing.sqlite")
    incumbent = lifecycle.open_trades[0]
    journal.create(
        SwitchIntent(
        switch_id="switch:trade-old:missing",
        incumbent_trade_id=incumbent.trade_id,
        incumbent_symbol=incumbent.symbol,
        candidate_opportunity_id="missing",
        candidate_symbol="ETHUSDT",
        state="close_pending",
            close_client_order_id="stable-close",
        )
    )
    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle,
        gateway=_Gateway(),
        journal=journal,
        close_and_confirm=_Close(),
        portfolio_provider=lambda: object(),
        evaluate_pair=lambda incumbent, candidate: _Decision(),
    )
    report = await PositionSwitchRecoveryWorker(coordinator).recover_pending()
    assert report.pending == 1
    assert report.resumed == 0
    assert "candidate payload" in report.errors[0]


@pytest.mark.asyncio
async def test_partial_close_and_cancelled_close_never_release_reservation_or_open(
    tmp_path,
):
    from aitos.trading.position_switching import CloseReceipt
    lifecycle = _Lifecycle()
    gateway = _Gateway()
    calls = iter(
        [
            CloseReceipt(False, "partial", 100.5, 0.4, False),
            CloseReceipt(False, "cancelled", 0.0, 0.0, False),
        ]
    )

    async def failing_close(*_):
        return next(calls)

    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle, gateway=gateway,
        journal=SQLiteSwitchJournal(tmp_path / "partial.sqlite"),
        close_and_confirm=failing_close,
        portfolio_provider=lambda: object(),
        evaluate_pair=lambda incumbent, candidate: _Decision(),
    )
    incumbent = lifecycle.open_trades[0]
    candidate = _candidate()
    assert (
        await coordinator.switch(
            incumbent=incumbent, candidate=candidate, decision=_Decision()
        )
        is None
    )
    assert await coordinator.switch(incumbent=incumbent, candidate=candidate, decision=_Decision()) is None
    assert gateway.released == []
    assert lifecycle.open_calls == 0
    assert lifecycle.close_calls == 0


@pytest.mark.asyncio
async def test_close_timeout_keeps_stable_intent_for_restart_reconciliation(tmp_path):
    from aitos.trading.position_switching import CloseReceipt

    lifecycle = _Lifecycle()
    gateway = _Gateway()
    journal = SQLiteSwitchJournal(tmp_path / "timeout.sqlite")
    calls = []

    async def close_with_timeout_then_fill(trade, client_order_id, existing_order_id):
        calls.append((client_order_id, existing_order_id))
        if len(calls) == 1:
            raise TimeoutError("transport timed out after exchange acceptance")
        return CloseReceipt(True, "exchange-order-42", 101.0, trade.quantity, True)

    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle, gateway=gateway, journal=journal,
        close_and_confirm=close_with_timeout_then_fill,
        portfolio_provider=lambda: object(),
        evaluate_pair=lambda incumbent, candidate: _Decision(),
    )
    incumbent = lifecycle.open_trades[0]
    candidate = _candidate()
    with pytest.raises(TimeoutError):
        await coordinator.switch(
            incumbent=incumbent, candidate=candidate, decision=_Decision()
        )
    assert journal.list_incomplete()[0].state == "close_submitting"

    report = await PositionSwitchRecoveryWorker(coordinator).recover_pending()
    assert report.resumed == 1
    assert calls[0][0] == calls[1][0]  # stable client order id across restart
    assert lifecycle.open_calls == 1


@pytest.mark.asyncio
async def test_repeated_recovery_event_does_not_duplicate_completed_switch(tmp_path):
    lifecycle = _Lifecycle()
    gateway = _Gateway()
    journal = SQLiteSwitchJournal(tmp_path / "duplicate-event.sqlite")
    coordinator = PositionSwitchCoordinator(
        lifecycle=lifecycle, gateway=gateway, journal=journal,
        close_and_confirm=_Close(), portfolio_provider=lambda: object(),
        evaluate_pair=lambda incumbent, candidate: _Decision(),
    )
    incumbent = lifecycle.open_trades[0]
    candidate = _candidate()
    await coordinator.switch(
        incumbent=incumbent, candidate=candidate, decision=_Decision()
    )
    worker = PositionSwitchRecoveryWorker(coordinator)
    first = await worker.recover_pending()
    second = await worker.recover_pending()
    assert first.scanned == 0
    assert second.scanned == 0
    assert lifecycle.close_calls == 1
    assert lifecycle.open_calls == 1
