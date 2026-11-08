"""Synthetic Binance-shaped records only; no credentials/provider requests."""

import copy
from datetime import datetime, timezone
from decimal import localcontext
import pytest
from aitos.exchange.binance_account_evidence import (
    adapt_recorded_account,
    reconcile_recorded_account,
)
from aitos.trading.account_reconciliation import reconcile_accounts

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)


def bundle():
    return dict(
        schema_version=1,
        account="synthetic",
        mode="testnet",
        server_time_ms=MS,
        observed_at=NOW.isoformat(),
        position_mode="ONE_WAY",
        income_start_ms=MS - 1000,
        income_end_ms=MS,
        page_size=2,
        income_pages=[
            [
                dict(
                    incomeType="COMMISSION",
                    tranId="9007199254740993",
                    time=MS,
                    income="-0.000000000000000001",
                    asset="USDT",
                    private_extra="must-not-export",
                )
            ]
        ],
        balances=[
            dict(asset="USDT", balance="100", availableBalance="105", crossUnPnl="5")
        ],
        positions=[
            dict(
                symbol="BTCUSDT",
                positionSide="BOTH",
                positionAmt="-0.000000000000000001",
            )
        ],
    )


def test_conversion_match_no_mutation_no_float_no_authority():
    raw = bundle()
    before = copy.deepcopy(raw)
    observed = adapt_recorded_account(raw, now=NOW)
    assert raw == before and observed["schema_version"] == 2
    assert observed["positions"] == [
        dict(symbol="BTCUSDT", side="SHORT", quantity="0.000000000000000001")
    ]
    assert observed["balances"][0]["available"] == "105"
    assert "must-not-export" not in str(observed)
    result = reconcile_recorded_account(observed, raw, now=NOW)
    assert (
        result["matched"]
        and not result["production_certified"]
        and not result["live_authorized"]
    )


def test_hedge_legs_do_not_net_out_and_zero_rows_do_not_create_positions():
    raw = bundle()
    raw["position_mode"] = "HEDGE"
    raw["positions"] = [
        dict(symbol="BTCUSDT", positionSide="LONG", positionAmt="2"),
        dict(symbol="BTCUSDT", positionSide="SHORT", positionAmt="-2"),
        dict(symbol="ETHUSDT", positionSide="LONG", positionAmt="0"),
    ]
    value = adapt_recorded_account(raw, now=NOW)
    assert len(value["positions"]) == 2
    missing = copy.deepcopy(value)
    missing["positions"] = []
    assert reconcile_accounts(missing, value, now=NOW)["status"] == "mismatch"


@pytest.mark.parametrize(
    "group,field,value",
    [
        ("positions", "positionAmt", 1.0),
        ("positions", "positionAmt", "NaN"),
        ("positions", "positionSide", "LONG"),
        ("positions", "positionAmt", "1e-18"),
        ("balances", "balance", True),
        ("balances", "asset", "usdt"),
        ("income", "tranId", True),
        ("income", "tranId", 9007199254740992.0),
        ("income", "time", True),
        ("income", "income", "Infinity"),
        ("income", "incomeType", "UNKNOWN"),
        ("income", "time", MS - 1001),
        ("income", "asset", "BTC/USDT"),
    ],
)
def test_bad_provider_rows_fail_closed(group, field, value):
    raw = bundle()
    rows = raw["income_pages"][0] if group == "income" else raw[group]
    rows[0][field] = value
    assert reconcile_recorded_account({}, raw, now=NOW)["status"] == "unavailable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "live"),
        ("schema_version", True),
        ("page_size", True),
        ("page_size", 0),
        ("income_pages", []),
        ("income_pages", {}),
        ("income_start_ms", MS + 1),
        ("income_end_ms", MS + 1),
        ("server_time_ms", True),
        ("position_mode", "UNKNOWN"),
        ("observed_at", "2026-11-06T11:58:00Z"),
    ],
)
def test_bad_recording_envelope_unavailable(field, value):
    raw = bundle()
    raw[field] = value
    assert reconcile_recorded_account({}, raw, now=NOW)["status"] == "unavailable"


def test_full_page_requires_short_terminal_page_and_duplicate_pages_rejected():
    raw = bundle()
    raw["page_size"] = 1
    with pytest.raises(ValueError):
        adapt_recorded_account(raw, now=NOW)
    raw["income_pages"].append([])
    assert len(adapt_recorded_account(raw, now=NOW)["receipts"]) == 1
    raw["income_pages"].insert(1, copy.deepcopy(raw["income_pages"][0]))
    with pytest.raises(ValueError):
        adapt_recorded_account(raw, now=NOW)


def test_same_tran_id_different_income_type_is_distinct_but_duplicate_identity_is_not():
    raw = bundle()
    row = copy.deepcopy(raw["income_pages"][0][0])
    row["incomeType"] = "FUNDING_FEE"
    raw["income_pages"][0].append(row)
    raw["income_pages"].append([])
    assert len(adapt_recorded_account(raw, now=NOW)["receipts"]) == 2
    raw["income_pages"][0][1]["incomeType"] = "COMMISSION"
    with pytest.raises(ValueError):
        adapt_recorded_account(raw, now=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("position_mode", "HEDGE"),
        ("receipt_window", dict(start_ms=MS - 999, end_ms=MS)),
        ("schema_version", 1),
    ],
)
def test_context_mismatch_never_reports_matched(field, value):
    raw = bundle()
    expected = adapt_recorded_account(raw, now=NOW)
    expected[field] = value
    assert reconcile_recorded_account(expected, raw, now=NOW)["status"] == "unavailable"


def test_negative_margin_balances_are_exact_data_not_silently_zeroed():
    raw = bundle()
    raw["balances"][0].update(balance="-1", availableBalance="-3")
    expected = adapt_recorded_account(raw, now=NOW)
    assert expected["balances"][0]["total"] == "-1"
    assert reconcile_recorded_account(expected, raw, now=NOW)["matched"]


def test_position_copy_abs_does_not_round_under_decimal_context():
    raw = bundle()
    raw["positions"][0]["positionAmt"] = "-123456789012345678901234.123456789012345678"
    with localcontext() as context:
        context.prec = 6
        value = adapt_recorded_account(raw, now=NOW)
    assert (
        value["positions"][0]["quantity"]
        == "123456789012345678901234.123456789012345678"
    )


@pytest.mark.parametrize("group", ["positions", "balances"])
def test_duplicate_account_rows_rejected_even_zero_positions(group):
    raw = bundle()
    if group == "positions":
        raw[group][0]["positionAmt"] = "0"
    raw[group] *= 2
    with pytest.raises(ValueError):
        adapt_recorded_account(raw, now=NOW)


def test_offline_cli_sanitizes_duplicate_json_and_secret_content(tmp_path, capsys):
    from aitos.exchange.binance_account_evidence import main

    expected = tmp_path / "expected.json"
    recorded = tmp_path / "raw.json"
    expected.write_text("{}")
    recorded.write_text('{"account":"private-do-not-echo","account":"second"}')
    assert main([str(expected), str(recorded)]) == 3
    assert "private-do-not-echo" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "body", ['{"x":1,"x":2}', '{"x":{"y":1,"y":2}}', '{"x":NaN}', '{"x":Infinity}']
)
def test_shared_strict_json_loader_rejects_hidden_fields_and_nonfinite(tmp_path, body):
    from aitos.trading.account_reconciliation import read_snapshot_file

    file = tmp_path / "evidence.json"
    file.write_text(body)
    with pytest.raises(ValueError):
        read_snapshot_file(file)


def test_normalized_one_way_input_cannot_hide_opposite_legs():
    raw = bundle()
    expected = adapt_recorded_account(raw, now=NOW)
    expected["positions"].append(
        dict(symbol="BTCUSDT", side="LONG", quantity="0.000000000000000001")
    )
    assert reconcile_accounts(expected, expected, now=NOW)["status"] == "unavailable"
