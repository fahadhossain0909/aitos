from datetime import datetime, timedelta, timezone

import pytest

from aitos.exchange.binance_pending_exposure import (
    attach_pending_exposures,
    pending_exposures_from_open_orders,
)
from aitos.risk.models import PortfolioState, PositionExposure

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def order(order_id=1, **overrides):
    value = {
        "orderId": order_id,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "positionSide": "BOTH",
        "type": "LIMIT",
        "status": "NEW",
        "origQty": "2.0",
        "executedQty": "0.5",
        "reduceOnly": False,
        "closePosition": False,
    }
    value.update(overrides)
    return value


def convert(orders, **kwargs):
    defaults = dict(
        mark_prices={"BTCUSDT": "100"},
        stop_prices={("BTCUSDT", "LONG"): "95", ("BTCUSDT", "SHORT"): "105"},
        leverage_by_symbol={"BTCUSDT": "3"},
        orders_observed_at=NOW,
        risk_inputs_observed_at=NOW,
        now=NOW,
    )
    defaults.update(kwargs)
    return pending_exposures_from_open_orders(orders, **defaults)


def test_partial_fill_projects_only_remaining_linear_contract_exposure():
    result = convert([order()])

    assert len(result) == 1
    assert result[0].order_id == "binance:1"
    assert result[0].symbol == "BTCUSDT"
    assert result[0].side == "LONG"
    assert result[0].remaining_notional_usd == 150.0
    assert result[0].risk_amount_usd == 7.5
    assert result[0].leverage == 3.0


def test_reduce_only_close_position_and_hedge_closing_legs_are_excluded():
    result = convert(
        [
            order(1, reduceOnly=True),
            order(2, closePosition=True),
            order(3, side="SELL", positionSide="LONG"),
            order(4, side="BUY", positionSide="SHORT"),
        ]
    )

    assert result == ()


def test_hedge_mode_open_short_is_counted_as_short():
    result = convert([order(side="SELL", positionSide="SHORT")])

    assert result[0].side == "SHORT"
    assert result[0].risk_amount_usd == 7.5


def test_same_symbol_open_position_and_pending_order_keep_one_slot():
    portfolio = PortfolioState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        positions=(PositionExposure("BTCUSDT", 100, 1),),
    )
    projected = attach_pending_exposures(
        portfolio,
        [order()],
        mark_prices={"BTCUSDT": "100"},
        stop_prices={("BTCUSDT", "LONG"): "95"},
        leverage_by_symbol={"BTCUSDT": "3"},
        orders_observed_at=NOW,
        risk_inputs_observed_at=NOW,
        now=NOW,
    )

    assert projected is not portfolio
    assert projected.projected_open_position_count == 1
    assert projected.gross_exposure_usd == 250.0


@pytest.mark.parametrize(
    "override",
    [
        {"status": "CANCELED"},
        {"side": "HOLD"},
        {"positionSide": "UNKNOWN"},
        {"type": "UNKNOWN"},
        {"origQty": "NaN"},
        {"executedQty": "3"},
        {"reduceOnly": "maybe"},
        {"reduceOnly": None},
        {"closePosition": None},
        {"positionSide": None},
    ],
)
def test_invalid_order_rows_fail_closed(override):
    with pytest.raises(ValueError):
        convert([order(**override)])


def test_missing_or_wrong_side_stop_fails_closed():
    with pytest.raises(ValueError, match="missing authoritative"):
        convert([order()], stop_prices={})
    with pytest.raises(ValueError, match="wrong side"):
        convert([order()], stop_prices={("BTCUSDT", "LONG"): "101"})


def test_missing_leverage_or_mark_fails_closed():
    with pytest.raises(ValueError, match="missing authoritative"):
        convert([order()], leverage_by_symbol={})
    with pytest.raises(ValueError, match="missing authoritative"):
        convert([order()], mark_prices={})


def test_stale_future_or_incoherent_snapshots_fail_closed():
    with pytest.raises(ValueError, match="stale or incoherent"):
        convert([order()], orders_observed_at=NOW - timedelta(seconds=6))
    with pytest.raises(ValueError, match="stale or incoherent"):
        convert([order()], risk_inputs_observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="stale or incoherent"):
        convert([order()], risk_inputs_observed_at=NOW - timedelta(seconds=3))


def test_duplicate_order_ids_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        convert([order(7), order(7)])
