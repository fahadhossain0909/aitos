"""Capital cannot expire/release while confirmed or uncertain exposure is owned."""

from datetime import datetime, timezone

import pytest

from aitos.execution.intent_store import ExecutionUncertainError
from aitos.execution.order_executor import SimulatedOrderExecutor
from aitos.intelligence.capital_protection import CapitalReservation, Reservation
from aitos.models.trade import Opportunity, TradeSide
from aitos.risk.models import PortfolioState
from aitos.trading.lifecycle import TradeLifecycle


def opportunity():
    return Opportunity(
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_price=100,
        stop_loss_price=98,
        take_profit_levels=[106],
        confidence=0.9,
        strategy_id="isolated",
        rationale="ownership regression",
        detected_at=datetime.now(timezone.utc).isoformat(),
    )


def test_pinned_capital_survives_authorization_lease_expiry(tmp_path, monkeypatch):
    import aitos.intelligence.capital_protection as module

    now = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    path = tmp_path / "capital.sqlite3"
    owner = CapitalReservation(path, lease_seconds=5)
    reservation = Reservation("BTCUSDT", 100, 10)
    assert owner.reserve(
        reservation,
        available_capital_usd=1000,
        available_risk_usd=100,
        capital_limit_usd=1000,
        risk_limit_usd=100,
    )
    assert owner.pin("BTCUSDT")
    now[0] += 1000
    assert owner.renew("BTCUSDT")
    now[0] += 1000
    assert owner.get("BTCUSDT") == reservation
    other = CapitalReservation(path, lease_seconds=5)
    assert not other.reserve(
        reservation,
        available_capital_usd=1000,
        available_risk_usd=100,
        capital_limit_usd=1000,
        risk_limit_usd=100,
    )
    assert owner.release("BTCUSDT") == reservation


@pytest.mark.parametrize("pin_failure", [False, True])
async def test_uncertain_submission_retains_capital(
    event_bus, risk_engine, monkeypatch, pin_failure
):
    if pin_failure:
        from aitos.intelligence.capital_gateway import CapitalGateway

        def fail_pin(*args):
            raise OSError("isolated retention reaffirmation failure")

        monkeypatch.setattr(CapitalGateway, "pin", fail_pin)

    class Uncertain(SimulatedOrderExecutor):
        async def submit_order(self, request):
            raise ExecutionUncertainError(
                request.symbol, request.client_order_id, "offline ACK uncertainty"
            )

    lifecycle = TradeLifecycle(event_bus, risk_engine, Uncertain())
    await lifecycle.initialize({})
    portfolio = PortfolioState(
        equity_usd=10000, peak_equity_usd=10000, volatility_percentile=30
    )
    try:
        with pytest.raises(ExecutionUncertainError):
            await lifecycle.submit_opportunity(opportunity(), portfolio)
        gateway = lifecycle._aitos_capital_gateway
        assert gateway.reservation.get("BTCUSDT") is not None
    finally:
        await lifecycle.shutdown()
