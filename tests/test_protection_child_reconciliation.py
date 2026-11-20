from decimal import Decimal

import pytest

from aitos.execution.protection_child_reconciliation import (
    ProtectionChildReconciliationError,
    calculate_protection_child_fill_delta,
)


def snapshots(*, status="PARTIALLY_FILLED", executed="0.4", average="101.5"):
    algo = {
        "algoId": 123,
        "actualOrderId": 456,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "algoStatus": "TRIGGERED",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "closePosition": False,
        "reduceOnly": True,
    }
    child = {
        "orderId": 456,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "status": status,
        "executedQty": executed,
        "avgPrice": average,
        "cumQuote": "40.6" if executed == "0.4" else "30.45",
        "origQty": "1.0",
        "positionSide": "BOTH",
    }
    return algo, child


def test_child_fill_delta_uses_cumulative_quantity_and_is_idempotent():
    algo, child = snapshots()
    first = calculate_protection_child_fill_delta(
        algo,
        child,
        expected_symbol="BTCUSDT",
        expected_side="SELL",
        expected_algo_id="123",
        previously_applied_quantity="0",
        previously_applied_quote="0",
    )
    assert first.delta_quantity == Decimal("0.4")
    assert first.delta_quote == Decimal("40.6")
    assert first.average_price == Decimal("101.5")
    replay = calculate_protection_child_fill_delta(
        algo,
        child,
        expected_symbol="BTCUSDT",
        expected_side="SELL",
        expected_algo_id="123",
        previously_applied_quantity="0.4",
        previously_applied_quote="40.6",
    )
    assert replay.delta_quantity == 0
    assert replay.has_new_fill is False


@pytest.mark.parametrize(
    "field,value",
    [("symbol", "ETHUSDT"), ("side", "BUY"), ("orderId", 999)],
)
def test_child_identity_mismatch_is_rejected(field, value):
    algo, child = snapshots()
    child[field] = value
    with pytest.raises(ProtectionChildReconciliationError):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0",
        )


def test_cumulative_fill_regression_is_rejected():
    algo, child = snapshots(executed="0.3")
    with pytest.raises(ProtectionChildReconciliationError, match="regressed"):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0.4",
        )


def test_filled_status_requires_full_original_quantity():
    algo, child = snapshots(status="FILLED", executed="0.4")
    with pytest.raises(ProtectionChildReconciliationError, match="original"):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0",
        )

 
 
def test_child_fill_uses_incremental_quote_not_lifetime_average():
    algo, child = snapshots(executed="0.6", average="102.0")
    child["cumQuote"] = "61.2"
    delta = calculate_protection_child_fill_delta(
        algo,
        child,
        expected_symbol="BTCUSDT",
        expected_side="SELL",
        expected_algo_id="123",
        previously_applied_quantity="0.4",
        previously_applied_quote="40.6",
        expected_position_side="BOTH",
    )
    assert delta.delta_quantity == Decimal("0.2")
    assert delta.delta_quote == Decimal("20.6")
    assert delta.average_price == Decimal("103")


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("algoStatus", "NEW", "not confirmed triggered"),
        ("actualOrderId", "999", "linkage mismatch"),
        ("orderType", "LIMIT", "unsupported protective"),
        ("positionSide", "LONG", "position-side mismatch"),
    ],
)
def test_algo_identity_and_state_are_fail_closed(field, value, reason):
    algo, child = snapshots()
    algo[field] = value
    with pytest.raises(ProtectionChildReconciliationError, match=reason):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0",
            expected_position_side="BOTH",
        )


def test_cumulative_quote_regression_is_rejected():
    algo, child = snapshots()
    with pytest.raises(ProtectionChildReconciliationError, match="quote regressed"):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0.3",
            previously_applied_quote="41",
        )


def test_executed_quantity_cannot_exceed_child_original_quantity():
    algo, child = snapshots(executed="1.1")
    child["cumQuote"] = "111.65"
    with pytest.raises(ProtectionChildReconciliationError, match="exceeds"):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0",
        )

def test_child_reduce_only_semantics_are_enforced_when_requested():
    algo, child = snapshots()
    with pytest.raises(ProtectionChildReconciliationError, match="reduce-only"):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0",
            expected_position_side="BOTH",
            expected_reduce_only=False,
        )


def test_child_original_quantity_must_match_durable_protection_request():
    algo, child = snapshots()
    child["origQty"] = "2.0"
    with pytest.raises(
        ProtectionChildReconciliationError,
        match="does not match durable protective intent",
    ):
        calculate_protection_child_fill_delta(
            algo,
            child,
            expected_symbol="BTCUSDT",
            expected_side="SELL",
            expected_algo_id="123",
            previously_applied_quantity="0",
            expected_original_quantity="1.0",
        )
