"""Offline inventory analysis is never recovery authorization."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from aitos.execution.intent_store import ExecutionIntentStore
from aitos.execution.order_executor import OrderRequest
from aitos.execution.protection_audit import audit_protection_inventory
from aitos.execution.recovery import read_protection_audit
from aitos.models.trade import TradeSide

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def inputs():
    request = dict(
        symbol="BTCUSDT",
        side="LONG",
        quantity=1,
        reference_price=90,
        order_type="STOP_MARKET",
        reduce_only=True,
    )
    rows = [
        dict(
            scope="account",
            client_id="cid",
            state="PROTECTED",
            request_json=json.dumps(request),
            result_json=json.dumps(dict(order_id="algo:123")),
        )
    ]
    evidence = dict(
        scope="account",
        complete=True,
        captured_at=NOW.isoformat(),
        symbols=["BTCUSDT"],
        orders=[
            dict(
                clientAlgoId="cid",
                algoId=123,
                symbol="BTCUSDT",
                side="SELL",
                positionSide="BOTH",
                reduceOnly=True,
                orderType="STOP_MARKET",
                algoStatus="NEW",
                quantity="1.000",
                triggerPrice="90.00",
            )
        ],
    )
    return rows, evidence


def audit(rows, evidence):
    return audit_protection_inventory(rows, evidence, scope="account", now=NOW)


def test_match_is_candidate_not_authorization():
    report = audit(*inputs())
    assert report["findings"][0]["status"] == "unapplied_match"
    assert report["safe_to_resume"] is report["account_exposure_verified"] is False
    assert report["automatic_actions"] == []


def test_missing_order_never_means_no_exposure():
    rows, evidence = inputs()
    evidence["orders"] = []
    result = audit(rows, evidence)["findings"][0]
    assert result["status"] == "not_in_open_inventory"
    assert "child" in result["reasons"][0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("side", "BUY"),
        ("positionSide", "SHORT"),
        ("reduceOnly", False),
        ("quantity", "0.9"),
        ("quantity", "NaN"),
        ("triggerPrice", "91"),
        ("orderType", "TAKE_PROFIT_MARKET"),
        ("algoStatus", "FINISHED"),
    ],
)
def test_contract_mismatch_never_accepted(field, value):
    rows, evidence = inputs()
    evidence["orders"][0][field] = value
    assert audit(rows, evidence)["findings"][0]["status"] == "mismatch"


def test_unowned_order_is_visible():
    rows, evidence = inputs()
    evidence["orders"][0]["clientAlgoId"] = "orphan"
    assert {f["status"] for f in audit(rows, evidence)["findings"]} == {
        "unowned_order",
        "not_in_open_inventory",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "another-account"),
        ("complete", False),
        ("symbols", ["ETHUSDT"]),
        ("captured_at", "invalid"),
        ("captured_at", "2026-11-07T12:00:00"),
        ("captured_at", (NOW - timedelta(seconds=121)).isoformat()),
        ("captured_at", (NOW + timedelta(seconds=1)).isoformat()),
    ],
)
def test_invalid_envelopes_fail_closed(field, value):
    rows, evidence = inputs()
    evidence[field] = value
    with pytest.raises(ValueError):
        audit(rows, evidence)


def test_duplicate_ids_fail_closed():
    rows, evidence = inputs()
    evidence["orders"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        audit(rows, evidence)


def test_hedge_match_does_not_require_unsupported_reduce_only_flag():
    rows, evidence = inputs()
    evidence["orders"][0]["positionSide"] = "LONG"
    evidence["orders"][0]["reduceOnly"] = False
    assert audit(rows, evidence)["findings"][0]["status"] == "unapplied_match"


def test_durable_identity_mismatch_is_blocked():
    rows, evidence = inputs()
    evidence["orders"][0]["algoId"] = 456
    assert (
        "durable algo identity mismatch"
        in audit(rows, evidence)["findings"][0]["reasons"]
    )


def test_audit_does_not_mutate_inputs():
    values = inputs()
    before = copy.deepcopy(values)
    audit(*values)
    assert values == before


def test_read_only_sqlite_audit_never_clears_intents(tmp_path):
    journal = ExecutionIntentStore(tmp_path / "journal.sqlite", scope="account")
    journal.begin(
        OrderRequest("BTCUSDT", TradeSide.LONG, 1, 90, "STOP_MARKET", "cid", True)
    )
    _, evidence = inputs()
    evidence["captured_at"] = datetime.now(timezone.utc).isoformat()
    report = read_protection_audit(journal.path, "account", evidence)
    assert report["findings"][0]["status"] == "unapplied_match"
    assert journal.unresolved()[0]["state"] == "SUBMITTING"
