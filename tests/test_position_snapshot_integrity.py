"""Read-only account/local quantity matching, including hedge and unknown positions."""

from types import SimpleNamespace

import pytest

from aitos.execution.position_integrity import assert_position_snapshot_matches
from aitos.models.trade import TradeSide


def local(side=TradeSide.LONG, quantity=1, symbol="BTCUSDT"):
    return SimpleNamespace(symbol=symbol, side=side, quantity=quantity)


def row(side="BOTH", quantity="1", symbol="BTCUSDT"):
    return dict(symbol=symbol, positionSide=side, positionAmt=quantity)


@pytest.mark.parametrize("side,qty", [(TradeSide.LONG, "1"), (TradeSide.SHORT, "-1")])
def test_one_way_matching_exposure(side, qty):
    assert_position_snapshot_matches(
        [local(side)], [row(quantity=qty)], hedge_mode=False
    )


def test_matching_hedged_positions_are_not_netted():
    assert_position_snapshot_matches(
        [local(), local(TradeSide.SHORT)],
        [row("LONG"), row("SHORT", "-1")],
        hedge_mode=True,
    )


def test_multiple_local_lots_compare_aggregate_quantity():
    assert_position_snapshot_matches(
        [local(quantity=0.4), local(quantity=0.6)], [row()], hedge_mode=False
    )


@pytest.mark.parametrize(
    "rows", [[], [row(quantity="2")], [row(quantity="-1")], [row(symbol="ETHUSDT")]]
)
def test_missing_wrong_quantity_direction_or_unknown_symbol_blocks(rows):
    with pytest.raises(ValueError):
        assert_position_snapshot_matches([local()], rows, hedge_mode=False)


def test_manual_untracked_position_blocks_even_with_empty_local_portfolio():
    with pytest.raises(ValueError):
        assert_position_snapshot_matches([], [row()], hedge_mode=False)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", None, True])
def test_non_finite_or_non_numeric_venue_quantity_blocks(bad):
    with pytest.raises(ValueError):
        assert_position_snapshot_matches(
            [local()], [row(quantity=bad)], hedge_mode=False
        )


@pytest.mark.parametrize(
    "rows", [None, {}, [None], [{}], [row(), row()], [row("LONG")]]
)
def test_malformed_snapshot_is_not_healthy(rows):
    with pytest.raises(ValueError):
        assert_position_snapshot_matches([local()], rows, hedge_mode=False)


@pytest.mark.parametrize("side,qty", [("LONG", "-1"), ("SHORT", "1"), ("BOTH", "1")])
def test_inconsistent_hedge_side_or_sign_blocks(side, qty):
    with pytest.raises(ValueError):
        assert_position_snapshot_matches([local()], [row(side, qty)], hedge_mode=True)


def test_zero_rows_do_not_create_phantom_exposure():
    assert_position_snapshot_matches([], [row(quantity="0")], hedge_mode=False)
