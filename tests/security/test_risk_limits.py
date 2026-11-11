from decimal import Decimal

import pytest

from aitos.security.risk_limits import AccountRiskSnapshot, SignerRiskLimits
from aitos.security.signing_policy import (
    IntentRejected,
    OrderIntentPolicy,
    validate_order_intent,
)


NOW = 1_800_000_000_000
POLICY = OrderIntentPolicy(
    account_alias="paper-primary",
    allowed_symbols=frozenset({"BTCUSDT"}),
    max_order_notional=Decimal("1000"),
)


def make_intent(**overrides):
    payload = {
        "schema_version": 1,
        "intent_id": "intent-risk-001",
        "nonce": "AAECAwQFBgcICQoLDA0ODw",
        "issued_at_ms": NOW - 100,
        "expires_at_ms": NOW + 10_000,
        "account_alias": "paper-primary",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "0.001",
        "limit_price": "65000",
        "reduce_only": False,
        "client_order_id": "aitos-risk-001",
    }
    payload.update(overrides)
    return validate_order_intent(payload, policy=POLICY, now_ms=NOW)


def snapshot(**overrides):
    values = {
        "account_alias": "paper-primary",
        "captured_at_ms": NOW,
        "gross_exposure": Decimal("100"),
        "symbol_exposure": {"BTCUSDT": Decimal("50")},
        "daily_loss": Decimal("10"),
        "order_count_window": 2,
        "positions": {"BTCUSDT": Decimal("0.01")},
    }
    values.update(overrides)
    return AccountRiskSnapshot(**values)


LIMITS = SignerRiskLimits(
    max_gross_exposure=Decimal("1000"),
    max_symbol_exposure=Decimal("500"),
    max_daily_loss=Decimal("100"),
    max_orders_per_window=10,
)


def test_fresh_account_evidence_passes_independent_risk_gate():
    LIMITS.authorize(make_intent(), snapshot(), now_ms=NOW)


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (None, "account_evidence_required"),
        (snapshot(account_alias="other"), "account_evidence_mismatch"),
        (snapshot(captured_at_ms=NOW - 6000), "account_evidence_stale"),
        (snapshot(daily_loss=Decimal("100")), "daily_loss_limit_reached"),
        (snapshot(order_count_window=10), "order_rate_limit_reached"),
        (snapshot(gross_exposure=Decimal("970")), "gross_exposure_limit_exceeded"),
        (
            snapshot(symbol_exposure={"BTCUSDT": Decimal("490")}),
            "symbol_exposure_limit_exceeded",
        ),
        (snapshot(gross_exposure=float("nan")), "account_evidence_invalid"),
    ],
)
def test_risk_gate_fails_closed(evidence, reason):
    with pytest.raises(IntentRejected, match=reason):
        LIMITS.authorize(make_intent(), evidence, now_ms=NOW)


def test_reduce_only_requires_matching_position_and_cannot_flip():
    reduce = make_intent(
        intent_id="intent-risk-reduce",
        nonce="AQECAwQFBgcICQoLDA0ODw",
        client_order_id="aitos-risk-reduce",
        side="SELL",
        quantity="0.005",
        reduce_only=True,
    )
    LIMITS.authorize(reduce, snapshot(), now_ms=NOW)
    with pytest.raises(IntentRejected, match="reduce_only_quantity_exceeds_position"):
        LIMITS.authorize(
            make_intent(
                intent_id="intent-risk-too-large",
                nonce="AgECAwQFBgcICQoLDA0ODw",
                client_order_id="aitos-risk-too-large",
                side="SELL",
                quantity="0.02",
                limit_price="40000",
                reduce_only=True,
            ),
            snapshot(),
            now_ms=NOW,
        )


def test_reduce_only_wrong_side_is_rejected():
    with pytest.raises(
        IntentRejected, match="reduce_only_side_would_increase_exposure"
    ):
        LIMITS.authorize(
            make_intent(
                intent_id="intent-risk-side",
                nonce="AwECAwQFBgcICQoLDA0ODw",
                client_order_id="aitos-risk-side",
                reduce_only=True,
            ),
            snapshot(),
            now_ms=NOW,
        )


def test_account_snapshot_copies_and_freezes_exposure_mappings():
    positions = {"BTCUSDT": Decimal("0.01")}
    exposure = {"BTCUSDT": Decimal("50")}
    evidence = snapshot(positions=positions, symbol_exposure=exposure)
    positions["BTCUSDT"] = Decimal("99")
    exposure.clear()
    assert evidence.positions["BTCUSDT"] == Decimal("0.01")
    assert evidence.symbol_exposure["BTCUSDT"] == Decimal("50")
    with pytest.raises(TypeError):
        evidence.positions["BTCUSDT"] = Decimal("1")
