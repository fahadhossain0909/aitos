from decimal import Decimal

import pytest

from aitos.security.signing_policy import (
    IntentRejected,
    OrderIntentPolicy,
    parse_order_intent_json,
    validate_order_intent,
)


NOW = 1_800_000_000_000
NONCE = "AAECAwQFBgcICQoLDA0ODw"
POLICY = OrderIntentPolicy(
    account_alias="paper-primary",
    allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    max_order_notional=Decimal("1000"),
)


def intent(**overrides):
    payload = {
        "schema_version": 1,
        "intent_id": "intent-001",
        "nonce": NONCE,
        "issued_at_ms": NOW - 100,
        "expires_at_ms": NOW + 10_000,
        "account_alias": "paper-primary",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "0.001",
        "limit_price": "65000.00",
        "reduce_only": False,
        "client_order_id": "aitos-order-001",
    }
    payload.update(overrides)
    return payload


def test_valid_limit_intent_returns_decimal_normalized_contract():
    result = validate_order_intent(intent(), policy=POLICY, now_ms=NOW)
    assert result.symbol == "BTCUSDT"
    assert result.quantity == Decimal("0.001")
    assert result.limit_price == Decimal("65000.00")
    assert result.notional == Decimal("65.00000")
    assert result.account_alias == "paper-primary"


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_secret": "must-never-be-accepted"},
        {"unexpected": "field"},
        {"quantity": 0.001},
        {"quantity": "1e-3"},
        {"quantity": "NaN"},
        {"quantity": "-1"},
        {"quantity": "0"},
        {"limit_price": "Infinity"},
        {"issued_at_ms": True},
        {"expires_at_ms": "1800000000010"},
        {"reduce_only": "false"},
        {"nonce": "short"},
        {"intent_id": "../unsafe"},
        {"schema_version": True},
    ],
)
def test_malformed_or_unknown_intents_fail_closed(overrides):
    with pytest.raises(IntentRejected):
        validate_order_intent(intent(**overrides), policy=POLICY, now_ms=NOW)


def test_expired_future_and_overlong_intents_are_rejected():
    for payload in (
        intent(expires_at_ms=NOW),
        intent(issued_at_ms=NOW + 10_000),
        intent(expires_at_ms=NOW + 31_000),
    ):
        with pytest.raises(IntentRejected):
            validate_order_intent(payload, policy=POLICY, now_ms=NOW)


def test_wrong_account_symbol_side_and_order_type_are_rejected():
    for payload in (
        intent(account_alias="production"),
        intent(symbol="DOGEUSDT"),
        intent(side="HOLD"),
        intent(order_type="STOP_LOSS"),
    ):
        with pytest.raises(IntentRejected):
            validate_order_intent(payload, policy=POLICY, now_ms=NOW)


def test_order_notional_cap_is_enforced():
    with pytest.raises(IntentRejected, match="order_notional_exceeded"):
        validate_order_intent(
            intent(quantity="1", limit_price="65000"),
            policy=POLICY,
            now_ms=NOW,
        )


def test_market_order_requires_explicit_policy_and_signer_price():
    market_policy = OrderIntentPolicy(
        account_alias="paper-primary",
        allowed_symbols=frozenset({"BTCUSDT"}),
        max_order_notional=Decimal("1000"),
        allowed_order_types=frozenset({"MARKET"}),
        allow_market_orders=True,
    )
    payload = intent(order_type="MARKET")
    payload.pop("limit_price")
    with pytest.raises(IntentRejected, match="trusted_market_price_required"):
        validate_order_intent(payload, policy=market_policy, now_ms=NOW)

    result = validate_order_intent(
        payload,
        policy=market_policy,
        now_ms=NOW,
        trusted_market_price=Decimal("65000"),
    )
    assert result.notional == Decimal("65.000")


def test_market_order_rejects_caller_supplied_price():
    market_policy = OrderIntentPolicy(
        account_alias="paper-primary",
        allowed_symbols=frozenset({"BTCUSDT"}),
        max_order_notional=Decimal("1000"),
        allowed_order_types=frozenset({"MARKET"}),
        allow_market_orders=True,
    )
    with pytest.raises(IntentRejected, match="market_intent_must_not_supply_price"):
        validate_order_intent(
            intent(order_type="MARKET"),
            policy=market_policy,
            now_ms=NOW,
            trusted_market_price=Decimal("65000"),
        )


def test_reduce_only_policy_can_be_disabled():
    policy = OrderIntentPolicy(
        account_alias="paper-primary",
        allowed_symbols=frozenset({"BTCUSDT"}),
        max_order_notional=Decimal("1000"),
        allow_reduce_only=False,
    )
    with pytest.raises(IntentRejected, match="reduce_only_not_allowed"):
        validate_order_intent(
            intent(reduce_only=True), policy=policy, now_ms=NOW
        )


def test_policy_rejects_unsafe_configuration():
    with pytest.raises(ValueError):
        OrderIntentPolicy(
            account_alias="paper-primary",
            allowed_symbols=frozenset(),
            max_order_notional=Decimal("1000"),
        )
    with pytest.raises(ValueError):
        OrderIntentPolicy(
            account_alias="paper-primary",
            allowed_symbols=frozenset({"BTCUSDT"}),
            max_order_notional=Decimal("1000"),
            allowed_order_types=frozenset({"MARKET"}),
        )



def test_json_parser_rejects_duplicate_keys_non_finite_and_oversized_body():
    with pytest.raises(IntentRejected, match="duplicate_json_key"):
        parse_order_intent_json('{"symbol":"BTCUSDT","symbol":"ETHUSDT"}')
    with pytest.raises(IntentRejected, match="non_finite_json_number"):
        parse_order_intent_json('{"quantity":NaN}')
    with pytest.raises(IntentRejected, match="intent_body_too_large"):
        parse_order_intent_json(" " * 4097)
    with pytest.raises(IntentRejected):
        parse_order_intent_json(b"\xff")
    with pytest.raises(IntentRejected, match="intent_must_be_json_object"):
        parse_order_intent_json("[]")


def test_json_parser_accepts_valid_utf8_object():
    parsed = parse_order_intent_json('{"symbol":"BTCUSDT","quantity":"0.01"}')
    assert parsed == {"symbol": "BTCUSDT", "quantity": "0.01"}
