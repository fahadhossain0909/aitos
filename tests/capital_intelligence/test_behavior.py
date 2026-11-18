from datetime import datetime, timezone
from aitos.capital_intelligence.behavior import assess_capital_behavior
from aitos.capital_intelligence.contracts import CapitalEvent


def item(event_id, sign):
    now = datetime.now(timezone.utc)
    return CapitalEvent(
        event_id,
        "test",
        "onchain",
        "crypto",
        "chain",
        "USDC",
        "transfer",
        now,
        now,
        100,
        "USDC",
        100,
        actor_id="wallet:a",
        counterparty_id="wallet:b",
        confidence=0.9,
        metadata={"flow_sign": sign},
    )


def test_unknown_direction_is_not_buy_or_sell():
    result = assess_capital_behavior("wallet:a", [item("1", 0)])
    assert result.accumulation_score == 0 and result.distribution_score == 0


def test_explicit_direction_drives_descriptive_score():
    result = assess_capital_behavior("wallet:a", [item("1", 1), item("2", 0)])
    assert result.accumulation_score == 0.5
    assert result.classification == "mixed_or_unclassified"


def test_behavior_accepts_chain_scoped_entity_identity():
    result = assess_capital_behavior("chain:wallet:a", [item("1", 1)])
    assert result.entity_id == "chain:wallet:a"
    assert result.accumulation_score == 1.0


def test_behavior_uses_inverse_flow_for_transfer_counterparty():
    result = assess_capital_behavior("chain:wallet:b", [item("1", -1)])
    assert result.net_flow_usd == 100
    assert result.accumulation_score == 1.0
