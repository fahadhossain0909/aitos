from decimal import Decimal

import pytest

from aitos.execution.protection_child_reconciliation import (
    ProtectionChildReconciliationError,
    calculate_protection_child_fill_delta,
)


def snapshots(*, status="PARTIALLY_FILLED", executed="0.4", average="101.5"):
    algo = {"algoId": 123, "actualOrderId": 456, "symbol": "BTCUSDT"}
    child = {
        "orderId": 456,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "status": status,
        "executedQty": executed,
        "avgPrice": average,
        "origQty": "1.0",
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
    )
    assert first.delta_quantity == Decimal("0.4")
    replay = calculate_protection_child_fill_delta(
        algo,
        child,
        expected_symbol="BTCUSDT",
        expected_side="SELL",
        expected_algo_id="123",
        previously_applied_quantity="0.4",
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
