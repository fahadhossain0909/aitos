import copy
from datetime import datetime, timezone

import pytest

from aitos.trading.account_reconciliation import AccountSnapshot, reconcile_accounts

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def snapshot():
    return dict(
        schema_version=1,
        provider="synthetic",
        account="account",
        mode="paper",
        as_of=NOW.isoformat(),
        observed_at=NOW.isoformat(),
        complete=True,
        balances=[dict(asset="USDT", total="100.000000000000000001", available="99")],
        positions=[
            dict(symbol="BTCUSDT", side="LONG", quantity="0.000000000000000001")
        ],
        receipts=[
            dict(receipt_id="fee-1", currency="USDT", amount="-0.000000000000000001")
        ],
    )


def test_exact_match_is_never_authority():
    left = snapshot()
    before = copy.deepcopy(left)
    result = reconcile_accounts(left, left, now=NOW)
    assert (
        result["matched"]
        and not result["live_authorized"]
        and not result["production_certified"]
    )
    assert left == before


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("balances", "total", "100.000000000000000002"),
        ("positions", "quantity", "0.000000000000000002"),
        ("positions", "side", "SHORT"),
        ("receipts", "amount", "0.000000000000000001"),
        ("receipts", "currency", "BTC"),
    ],
)
def test_single_smallest_decimal_or_scope_difference_is_not_hidden(group, field, value):
    left = snapshot()
    right = copy.deepcopy(left)
    right[group][0][field] = value
    result = reconcile_accounts(left, right, now=NOW)
    assert result["status"] == "mismatch" and result["differences"][group] > 0
    assert "account" not in str(result) and "fee-1" not in str(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("complete", False),
        ("schema_version", True),
        ("provider", "other"),
        ("account", "other"),
        ("mode", "live"),
        ("observed_at", "2026-11-06T11:58:00Z"),
        ("as_of", "2026-11-06T12:01:00Z"),
        ("positions", {}),
        ("receipts", None),
    ],
)
def test_incomplete_stale_or_wrong_scope_is_unavailable(field, value):
    left = snapshot()
    right = copy.deepcopy(left)
    right[field] = value
    result = reconcile_accounts(left, right, now=NOW)
    assert result["status"] == "unavailable" and not result["matched"]


@pytest.mark.parametrize(
    "bad", ["NaN", "Infinity", "1e-18", 1.0, True, "0." + "1" * 19, ""]
)
def test_decimal_values_never_coerce_float_or_exponents(bad):
    left = snapshot()
    right = copy.deepcopy(left)
    right["receipts"][0]["amount"] = bad
    assert reconcile_accounts(left, right, now=NOW)["status"] == "unavailable"


@pytest.mark.parametrize("group", ["balances", "positions", "receipts"])
def test_duplicate_identity_blocks_comparison(group):
    left = snapshot()
    right = copy.deepcopy(left)
    right[group] *= 2
    assert reconcile_accounts(left, right, now=NOW)["status"] == "unavailable"


def test_missing_receipt_detected_and_repr_does_not_contain_financials():
    left = snapshot()
    right = copy.deepcopy(left)
    right["receipts"] = []
    assert reconcile_accounts(left, right, now=NOW)["differences"]["receipts"] == 1
    assert "100.000" not in repr(AccountSnapshot.parse(left, now=NOW))


def test_offline_cli_does_not_disclose_bad_file_contents(tmp_path, capsys):
    from aitos.trading.account_reconciliation import main

    expected = tmp_path / "expected.json"
    observed = tmp_path / "observed.json"
    expected.write_text("malformed-secret-do-not-echo")
    observed.write_text("{}")
    assert main([str(expected), str(observed)]) == 3
    output = capsys.readouterr().out
    assert "unavailable" in output and "malformed-secret" not in output
